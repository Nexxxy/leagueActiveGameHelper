"""JSONL-Shard-Store fuer den Roh-Cache (Layer 0).

Warum: der Roh-Cache lag als ~142.000 EINZELDATEIEN unter `data/pipeline/`
(mastery ~65k, matches ~50k, timelines ~22k, matchids ~4,3k). Jede Datei kostet
NTFS-Open/Close plus Defender-Scan; Voll-Scans (`glob`, `peek`) ueber
zehntausende Dateien dauern Minuten. Der Layer-1-Match-Index
(`pipeline/matchindex.py`) hat dasselbe Problem fuer die Leseseite der
Aggregation bereits geloest - dieses Modul verallgemeinert das Muster
(append-only JSONL, tolerante Leser, atomarer Vollschrieb via tmp+replace) auf
Layer 0 und reduziert den Cache auf ~40 Shard-Dateien.

Layout::

    data/pipeline/store/matches/<patch>/<platform>.jsonl
                        timelines/<patch>/<platform>.jsonl
                        matchids/<patch>/<h>.jsonl
                        mastery/<h>.jsonl

Warum zwei Shard-Schluessel:

- matches/timelines shardden nach dem Platform-Praefix der Match-ID
  (`EUW1_123` -> `euw1`). Im Multi-Region-Betrieb schreibt damit **genau ein**
  Region-Worker-Thread je Shard - Write-Konflikte sind by design ausgeschlossen.
- matchids/mastery shardden nach `md5(puuid)[0]` (16 Shards). Hier schreiben
  mehrere Region-Threads in denselben Shard; ein per-Datei-Lock serialisiert
  (alle Worker sind Threads EINES Prozesses).

Record-Format (eine JSON-Zeile, append-only, last-wins pro id)::

    {"id": "EUW1_123", "t": 1753795200, "d": { ...Payload... }}
    {"id": "EUW1_124", "t": 1753795201, "skip": "prepatch_gate"}

`t` ist der Schreibzeitpunkt (Epoch-Sekunden) und ersetzt die Datei-mtime fuer
alle TTL-Pruefungen (`cacheio.fresh`-Semantik wandert damit in den Store). Ein
Update haengt einen neuen Record an; der juengste gewinnt, `compact()` raeumt
die toten Zeilen weg.

Random Access: beim ersten Zugriff auf einen Shard laeuft EIN sequentieller
Scan, der eine RAM-Map `id -> (offset, laenge, t, skip)` baut. Die id wird dabei
per Regex aus dem ZEILENANFANG geholt, nicht per Voll-`json.loads` - eine
Timeline-Zeile ist gern 100 KB+ gross, das Parsen aller Zeilen wuerde den
Vorteil auffressen. Deshalb schreibt der Store `id` garantiert als erstes Feld.
`get()` macht danach seek+read auf genau eine Zeile. Jeder weitere Zugriff
vergleicht nur noch die Dateigroesse mit der eigenen Scan-Marke und liest den
Zuwachs nach - so bleibt auch eine langlebige Instanz aktuell, ohne je wieder
alles zu lesen (`shared()` nutzt genau das fuer die Lesepfade der App).

Offset-Sidecar (`<shard>.idx`): der Vollscan kostet beim groessten Shard
(timelines euw1, ~9 GB) zweistellige Sekunden - fuer kurzlebige Prozesse
(Post-Game, App-History, status) der teuerste Einzelposten. Der Sidecar
persistiert die Offset-Map deshalb neben dem Shard und macht das Oeffnen zu
Millisekunden + Delta-Scan. Er ist reine WEGWERF-WARE wie der Layer-1-Index:
nie Wahrheit, immer aus dem Shard reproduzierbar, kaputt/veraltet/fehlend ->
Vollscan wie gehabt und danach Neuschrieb. Geschrieben wird NUR nach einem
vollendeten Vollscan, beim `bulk()`-Close, nach `compact()` und wenn der Delta
seit dem persistierten Stand `_IDX_REFRESH_BYTES` reisst - nie je Einzel-Put.
Details am Header-Format s. `_load_sidecar`.

Robustheit: Leser ueberspringen kaputte Zeilen wie `matchindex._load_index`
(Strg-C mitten im Append hinterlaesst hoechstens eine halbe LETZTE Zeile, weil
je Record ein `write()`+`flush()` erfolgt); `compact()` repariert die Datei.
Schreib-Handles sind kurzlebig (open-append-close), damit auch ein zweiter
Prozess (App-Postgame waehrend eines Pipeline-Laufs) anhaengen kann.

Alle I/O laeuft bewusst binaer: so sind die gespeicherten Offsets echte
Byte-Offsets und Windows uebersetzt kein `\\n` in `\\r\\n` (was jeden Offset
verschieben wuerde).
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .cacheio import DEFAULT_TTL_S
from .config import Config

STORE_DIR = "store"          # Unterordner unter cfg.cache_dir
HASH_SHARDS = 16             # md5-Hex-Nibble -> 0..f
PLATFORM_FALLBACK = "euw1"   # Match-ID ohne '_' (Legacy, s. harvest._on_platform)

# kind -> (patch-gebunden?, Shard-Strategie)
KINDS: dict[str, tuple[bool, str]] = {
    "matches":   (True,  "platform"),
    "timelines": (True,  "platform"),
    "matchids":  (True,  "hash"),
    "mastery":   (False, "hash"),
}


# --- Shard-Schluessel -------------------------------------------------------

def platform_shard(match_id: str) -> str:
    """'EUW1_7923765095' -> 'euw1'. IDs ohne '_' -> 'euw1'.

    Der Fallback spiegelt `harvest._on_platform`: vor der Multi-Region-Zeit
    kamen alle Matches aus EUW, entsprechend praefixlose Alt-IDs."""
    mid = str(match_id).strip()
    if "_" not in mid:
        return PLATFORM_FALLBACK
    return mid.split("_", 1)[0].lower()


def hash_shard(key: str) -> str:
    """PUUID -> erstes Hex-Zeichen von md5(puuid) (16 gleichmaessige Shards).

    md5 dient hier nur der Streuung, nicht der Sicherheit (daher
    `usedforsecurity=False`, sonst faellt der Aufruf auf FIPS-Builds um)."""
    digest = hashlib.md5(str(key).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[0]


def root_path(cfg: Config, kind: str, patch: str | None = None) -> Path:
    """Shard-Verzeichnis eines Kinds (`store/<kind>[/<patch>]`)."""
    base = cfg.cache_dir / STORE_DIR / kind
    return base / patch if patch else base


def patches(cfg: Config, kind: str) -> list[str]:
    """Patches, zu denen `kind` Shards hat - lexikografisch sortiert.

    Bewusst lexikografisch (nicht semantisch nach Versionsnummer): die
    Patch-Suche in `app/postgame/fetch._find_cached` lief bisher ueber
    `sorted(base.iterdir(), reverse=True)`, die Reihenfolge bleibt damit beim
    Umstieg auf den Store unveraendert."""
    patch_bound, _mode = _kind_spec(kind)
    if not patch_bound:
        raise ValueError(f"kind '{kind}' ist nicht patch-gebunden - patches() sinnlos.")
    base = cfg.cache_dir / STORE_DIR / kind
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def _kind_spec(kind: str) -> tuple[bool, str]:
    try:
        return KINDS[kind]
    except KeyError:
        raise ValueError(f"Unbekanntes kind '{kind}' - bekannt: "
                         f"{', '.join(sorted(KINDS))}.") from None


# --- Locks ------------------------------------------------------------------
# Ein RLock je Shard-DATEI (nicht je Store-Instanz): mehrere ShardStore-Objekte
# desselben Prozesses duerfen denselben Shard bedienen. Reentrant, weil `put`
# den Lock haelt und darin den Index-Aufbau anstoesst, der ihn erneut nimmt.

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    lock = _LOCKS.get(key)
    if lock is None:
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
    return lock


# --- Zeilen lesen / Kopf parsen --------------------------------------------

_HEAD_BYTES = 512   # reicht fuer '{"id": "<puuid 78>", "t": <n>, "d"' mit Reserve
_HEAD_RE = re.compile(
    r'^\{"id":\s*(?P<id>"(?:[^"\\]|\\.)*")\s*,\s*"t":\s*'
    r'(?P<t>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*,\s*"(?P<key>d|skip)"')


def _iter_lines(path: Path, start: int = 0):
    """(offset, zeile_ohne_newline, ende_offset) je nicht-leerer Zeile ab `start`.

    `ende_offset` ist der Beginn der NAECHSTEN Zeile; ist er gleich der
    Dateigroesse und die Zeile trug keinen Zeilenumbruch, war es eine
    abgeschnittene Schlusszeile (Strg-C mitten im Append) - erkennbar daran,
    dass `ende_offset - offset == len(zeile)`. Fehlende/unlesbare Datei ->
    leere Folge."""
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        if start:
            fh.seek(start)
        offset = start
        for raw in fh:
            begin = offset
            offset += len(raw)
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            yield begin, line, offset


def _parse_head(line: bytes):
    """(id, t, skip) allein aus dem Zeilenanfang - ohne die Zeile zu parsen.

    Das ist der Kern des billigen Scans: nur die ersten `_HEAD_BYTES` werden
    dekodiert und per Regex gelesen. None = Kopf passt nicht zum Store-Format
    (dann entscheidet der Voll-Parse)."""
    m = _HEAD_RE.match(line[:_HEAD_BYTES].decode("utf-8", "replace"))
    if m is None:
        return None
    try:
        rid = json.loads(m.group("id"))
    except json.JSONDecodeError:
        return None
    return rid, float(m.group("t")), m.group("key") == "skip"


def _parse_full(line: bytes):
    """(id, t, skip) per Voll-`json.loads`; None, wenn die Zeile kaputt ist."""
    try:
        rec = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or rec.get("id") is None:
        return None
    return rec["id"], float(rec.get("t") or 0.0), "skip" in rec


def _scan(path: Path, start: int = 0) -> tuple[dict, int]:
    """Sequentieller Shard-Scan ab `start` -> (index, gelesen_bis).

    index: `id -> (offset, laenge, t, skip)`, last-wins (spaetere Zeile
    ueberschreibt). `gelesen_bis` ist das Ende der letzten VOLLSTAENDIGEN
    Zeile - eine abgeschnittene Schlusszeile bleibt bewusst ausserhalb, damit
    der naechste (Delta-)Scan sie erneut ansieht, sobald der Schreiber sie
    fertiggestellt hat."""
    index: dict[str, tuple] = {}
    consumed = start
    for offset, line, end in _iter_lines(path, start):
        if end - offset == len(line):
            # Abgeschnittene letzte Zeile: kann einen GUELTIGEN Kopf und
            # trotzdem kaputten Rumpf haben -> immer voll parsen und NICHT
            # als gelesen markieren (der naechste Scan sieht sie erneut an).
            parsed = _parse_full(line)
            if parsed is not None:
                index[parsed[0]] = (offset, len(line), parsed[1], parsed[2])
            break
        consumed = end
        parsed = _parse_head(line) or _parse_full(line)
        if parsed is None:
            continue                             # kaputt -> compact repariert
        rid, t, skip = parsed
        index[rid] = (offset, len(line), t, skip)
    return index, consumed


def _dump(rec: dict) -> bytes:
    return (json.dumps(rec) + "\n").encode("utf-8")


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class _ShardIndex:
    """Offset-Map eines Shards plus die Byte-Marke, bis zu der gescannt wurde.

    `persisted` ist die Marke, die im Sidecar auf der Platte steht (0 = keiner):
    aus `scanned - persisted` ergibt sich, wie weit der Sidecar hinterherhinkt
    und wann sich ein Neuschrieb lohnt."""

    __slots__ = ("entries", "scanned", "persisted")

    def __init__(self, entries: dict, scanned: int, persisted: int = 0):
        self.entries = entries
        self.scanned = scanned
        self.persisted = persisted


# --- Offset-Sidecar ---------------------------------------------------------

IDX_SUFFIX = ".idx"                        # <shard>.jsonl -> <shard>.idx
_IDX_SCHEMA = 1
_SIG_BYTES = 4096                          # Groesse der beiden Signaturfenster
_IDX_REFRESH_BYTES = 64 * 1024 * 1024      # Delta, ab dem neu geschrieben wird


def _signature(path: Path, valid_bytes: int):
    """(sha1 der ersten 4 KB, sha1 der 4 KB VOR `valid_bytes`) oder None.

    None heisst: die Datei ist kuerzer als `valid_bytes` oder unlesbar - dann
    kann der Sidecar unmoeglich passen. Bewusst zwei kleine Fenster statt eines
    Vollhashes: 9 GB zu hashen waere teurer als der Vollscan, den der Sidecar
    gerade einspart. Fuer append-only-Shards reicht das: der Praefix
    identifiziert die Datei, das Tail-Fenster die Naht, an der der Delta-Scan
    ansetzt. Ein neu geschriebener Shard (compact) faellt damit auch dann auf,
    wenn er zufaellig groesser ist als `valid_bytes`."""
    if valid_bytes <= 0:
        return None
    head_len = min(_SIG_BYTES, valid_bytes)
    tail_at = max(0, valid_bytes - _SIG_BYTES)
    try:
        with path.open("rb") as fh:
            head = fh.read(head_len)
            fh.seek(tail_at)
            tail = fh.read(valid_bytes - tail_at)
    except OSError:
        return None
    if len(head) != head_len or len(tail) != valid_bytes - tail_at:
        return None                        # Datei kuerzer als behauptet
    return (hashlib.sha1(head, usedforsecurity=False).hexdigest(),
            hashlib.sha1(tail, usedforsecurity=False).hexdigest())


def _load_sidecar(idx_path: Path, shard_path: Path):
    """Sidecar laden und gegen den Shard verifizieren -> `_ShardIndex` | None.

    Format (JSONL): Kopfzeile
    `{"schema": 1, "valid_bytes": N, "records": M, "prefix_sha1": .., "tail_sha1": ..}`,
    danach je Record eine kompakte Liste `[id, offset, laenge, t]` (+ `1` als
    fuenftes Element bei Skip-Markern).

    Jede Unstimmigkeit - falsches Schema, Signatur-Mismatch, eine kaputte Zeile,
    abweichende Record-Zahl - verwirft den GANZEN Sidecar (Rueckgabe None) und
    fuehrt beim Aufrufer zum Vollscan. Teilweise uebernehmen waere gefaehrlich:
    die Scan-Marke behauptet Vollstaendigkeit bis `valid_bytes`, fehlende
    Eintraege wuerden also nie nachgelesen und ihre Records still verschwinden.

    Das Handle ist bewusst kurzlebig (`read_bytes`): auf Windows scheitert das
    `replace` eines Schreibers, solange ein anderer Prozess die Datei offen
    haelt."""
    try:
        raw = idx_path.read_bytes()
    except OSError:
        return None
    lines = raw.split(b"\n")
    try:
        header = json.loads(lines[0].decode("utf-8"))
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(header, dict) or header.get("schema") != _IDX_SCHEMA:
        return None
    valid, count = header.get("valid_bytes"), header.get("records")
    if not isinstance(valid, int) or not isinstance(count, int):
        return None
    sig = _signature(shard_path, valid)
    if sig is None or (header.get("prefix_sha1"), header.get("tail_sha1")) != sig:
        return None
    entries: dict = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None                    # halbe Zeile -> Sidecar unbrauchbar
        if not isinstance(row, list) or len(row) < 4:
            return None
        try:
            entries[row[0]] = (int(row[1]), int(row[2]), float(row[3]),
                               bool(row[4]) if len(row) > 4 else False)
        except (TypeError, ValueError):
            return None
    if len(entries) != count:
        return None
    return _ShardIndex(entries, valid, persisted=valid)


def _write_sidecar(idx_path: Path, shard_path: Path, entries: dict,
                   valid_bytes: int) -> bool:
    """Sidecar atomar schreiben (tmp+replace, PID im tmp-Namen) -> Erfolg?

    Der PID im tmp-Namen trennt zwei gleichzeitig schreibende Prozesse; das
    `replace` selbst entscheidet dann last-wins, beide Inhalte waeren gueltig.
    Scheitert es (auf Windows `PermissionError`, wenn ein Fremdprozess die
    Datei gerade offen hat), wird das Schreiben STILL uebersprungen: der Sidecar
    ist abgeleitete Ware, der naechste Lauf versucht es erneut. Kein Retry."""
    sig = _signature(shard_path, valid_bytes)
    if sig is None:
        return False
    tmp = idx_path.with_name(f"{idx_path.name}.tmp{os.getpid()}")
    header = {"schema": _IDX_SCHEMA, "valid_bytes": valid_bytes,
              "records": len(entries),
              "prefix_sha1": sig[0], "tail_sha1": sig[1]}
    try:
        with tmp.open("wb") as fh:
            fh.write(_dump(header))
            for rid, ent in entries.items():
                row = [rid, ent[0], ent[1], ent[2]]
                if ent[3]:
                    row.append(1)
                fh.write(_dump(row))
        tmp.replace(idx_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


# --- Store ------------------------------------------------------------------

class ShardStore:
    """Ein Kind des Roh-Caches (optional fuer einen Patch).

    Die Offset-Maps werden faul aufgebaut und danach INKREMENTELL fortgeschrieben:
    jeder Zugriff vergleicht die Dateigroesse mit der eigenen Scan-Marke und
    liest nur den Zuwachs nach (ein `stat` pro Zugriff). Damit sieht auch eine
    langlebige Instanz die Neuzugaenge fremder Schreiber - noetig, weil die App
    (Post-Game) anhaengt, waehrend ein Pipeline-Lauf liest. Schrumpft die Datei
    (compact/purge), wird komplett neu gescannt."""

    def __init__(self, cfg: Config, kind: str, patch: str | None = None):
        patch_bound, mode = _kind_spec(kind)
        if patch_bound and not patch:
            raise ValueError(f"kind '{kind}' ist patch-gebunden - `patch` fehlt.")
        if not patch_bound and patch:
            raise ValueError(f"kind '{kind}' kennt keine Patches - `patch` weglassen.")
        self.cfg = cfg
        self.kind = kind
        self.patch = patch
        self.root = root_path(cfg, kind, patch)
        self._mode = mode
        self._index: dict[str, _ShardIndex] = {}
        self._dir_ready = False
        # Diagnose-/Test-Haken: `full_scans` zaehlt die teuren Vollscans, die
        # der Sidecar gerade vermeiden soll (0 = jeder Shard kam aus dem
        # Sidecar), `sidecar_loads`/`sidecar_writes` die Gegenrichtung.
        self.stats = {"full_scans": 0, "sidecar_loads": 0, "sidecar_writes": 0}

    # --- intern ---

    def shard_of(self, id) -> str:
        """Shard-Name einer id (Platform-Praefix bzw. md5-Nibble)."""
        if self._mode == "platform":
            return platform_shard(id)
        return hash_shard(id)

    def path_of(self, shard: str) -> Path:
        return self.root / f"{shard}.jsonl"

    def idx_path_of(self, shard: str) -> Path:
        """Pfad des Offset-Sidecars eines Shards (`<shard>.idx`)."""
        return self.root / f"{shard}{IDX_SUFFIX}"

    def shards(self) -> list[str]:
        """Vorhandene Shard-Namen (sortiert); leer, wenn nichts geschrieben wurde.

        Bewusst ueber `*.jsonl`: die Sidecars (`*.idx`) sind abgeleitet und
        duerfen nie als Shard durchgehen."""
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    def _rebuild(self, shard: str) -> _ShardIndex:
        """Index eines Shards von Grund auf: erst der Sidecar, sonst Vollscan.

        Der Sidecar deckt nur bis `valid_bytes` ab - den Rest holt der normale
        Delta-Nachzug in `_state`. Aufrufer haelt den Shard-Lock."""
        path = self.path_of(shard)
        state = _load_sidecar(self.idx_path_of(shard), path)
        if state is not None:
            self.stats["sidecar_loads"] += 1
            return state
        entries, scanned = _scan(path)
        self.stats["full_scans"] += 1
        state = _ShardIndex(entries, scanned)
        self._persist(shard, state, force=True)
        return state

    def _persist(self, shard: str, state: _ShardIndex, force: bool = False) -> None:
        """Sidecar schreiben - `force` nach Vollscan/bulk-Close/compact, sonst
        nur, wenn der Delta seit dem persistierten Stand die Schwelle reisst.

        Ein leerer bzw. gar nicht vorhandener Shard bekommt keinen Sidecar -
        sonst legte schon ein `has()` auf einen nie geschriebenen Shard Dateien
        (und das Verzeichnis) an. Aufrufer haelt den Shard-Lock."""
        if state.scanned <= 0:
            return
        if not force and state.scanned - state.persisted < _IDX_REFRESH_BYTES:
            return
        if _write_sidecar(self.idx_path_of(shard), self.path_of(shard),
                          state.entries, state.scanned):
            state.persisted = state.scanned
            self.stats["sidecar_writes"] += 1

    def _state(self, shard: str) -> _ShardIndex:
        """Shard-Index, beim ersten Zugriff gebaut und danach per Delta
        nachgezogen (nur der Zuwachs seit dem letzten Scan wird gelesen)."""
        path = self.path_of(shard)
        state = self._index.get(shard)
        if state is not None and _size_of(path) == state.scanned:
            return state                          # unveraendert - haeufigster Fall
        with _lock_for(path):
            state = self._index.get(shard)
            size = _size_of(path)
            if state is None or size < state.scanned:
                state = self._rebuild(shard)      # neu oder geschrumpft
                self._index[shard] = state
            # Kein `elif`: kommt der Index frisch aus dem SIDECAR, deckt er nur
            # bis `valid_bytes` ab - der Zuwachs seither muss im selben Zug
            # nachgelesen werden.
            if size > state.scanned:
                grown, scanned = _scan(path, state.scanned)
                state.entries.update(grown)       # last-wins bleibt gewahrt
                state.scanned = scanned
                self._persist(shard, state)       # Schwellen-Nachzug
            return state

    def _entries(self, shard: str) -> dict:
        return self._state(shard).entries

    def _snapshot(self, shard: str) -> list:
        """(id, eintrag)-Liste unter dem Shard-Lock - sichere Iterationsbasis,
        auch wenn ein Nachbar-Thread gerade in denselben Shard schreibt."""
        with _lock_for(self.path_of(shard)):
            return list(self._state(shard).entries.items())

    def reload(self) -> None:
        """Verwirft alle Offset-Maps - der naechste Zugriff scannt komplett neu.

        Der Delta-Nachzug deckt Anhaengsel bereits ab; `reload()` ist fuer den
        Rest gedacht (in-place veraenderte oder ersetzte Dateien)."""
        self._index.clear()

    def _read_record(self, shard: str, ent: tuple, id) -> dict | None:
        """Genau eine Zeile per seek+read; None, wenn sie nicht (mehr) passt."""
        offset, length = ent[0], ent[1]
        try:
            with self.path_of(shard).open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(length)
        except OSError:
            return None
        if len(raw) != length:
            return None
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(rec, dict) or rec.get("id") != id:
            return None
        return rec

    def _append(self, id, rec: dict) -> None:
        data = _dump(rec)
        shard = self.shard_of(id)
        path = self.path_of(shard)
        with _lock_for(path):
            state = self._state(shard)
            if not self._dir_ready:
                self.root.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            with path.open("ab") as fh:
                fh.write(data)
                fh.flush()   # Strg-C hinterlaesst hoechstens eine halbe Zeile
                end = fh.tell()
            # tell() nach dem Write ist auch bei fremden Anhaengseln korrekt
            # (O_APPEND schreibt immer ans tatsaechliche Dateiende).
            start = end - len(data)
            state.entries[id] = (start, len(data) - 1,
                                 float(rec["t"]), "skip" in rec)
            if start == state.scanned:
                state.scanned = end
                # Schwellen-Nachzug (kein Schrieb je Put): in einem langen
                # Harvest-Lauf waechst der Shard nur ueber diesen Pfad, ohne
                # die Pruefung hier bliebe der Sidecar den ganzen Lauf lang auf
                # dem Eingangsstand stehen. `_persist` schreibt erst, wenn
                # _IDX_REFRESH_BYTES zusammengekommen sind.
                self._persist(shard, state)
            # Sonst hat ein FREMDER Schreiber dazwischen angehaengt: die Marke
            # bleibt stehen, der naechste Zugriff liest dessen Zeilen (und
            # unsere gleich mit) per Delta nach. Dann auch kein Sidecar-Schrieb:
            # er wuerde Eintraege jenseits von `valid_bytes` festschreiben.

    # --- Schreiben ---

    def put(self, id, payload, t: float | None = None) -> None:
        """Payload anhaengen (last-wins). `t` ueberschreibt den Zeitstempel -
        die Migration uebernimmt damit die mtime der Alt-Datei, sonst waere die
        TTL-Semantik nach dem Umzug zurueckgesetzt."""
        self._append(id, {"id": id, "t": time.time() if t is None else float(t),
                          "d": payload})

    def put_skip(self, id, reason: str, t: float | None = None) -> None:
        """Skip-Marker anhaengen (dauerhaft nicht abrufbar, nie erneut holen)."""
        self._append(id, {"id": id, "t": time.time() if t is None else float(t),
                          "skip": reason})

    def bulk(self) -> "_BulkWriter":
        """Sammelschreiber fuer VIELE Records am Stueck (Migration, Backfills).

        `put`/`put_skip` oeffnen je Record einmal die Shard-Datei - bei
        zehntausenden Records ist genau das der Flaschenhals (Datei-Open +
        Defender). Der Sammelschreiber haelt je Shard EIN Handle offen::

            with store.bulk() as w:
                w.put(mid, data, t=mtime)

        Nutzung als Kontextmanager (schliesst die Handles zuverlaessig)."""
        return _BulkWriter(self)

    # --- Lesen ---

    def has(self, id) -> bool:
        """Ist die id bekannt? Skip-Marker zaehlen als bekannt - genau wie
        frueher die Existenz der Skip-DATEI (sonst wuerde erneut gefetcht)."""
        return id in self._entries(self.shard_of(id))

    def get(self, id):
        """Payload der juengsten Zeile, `{"skip": <grund>}` bei Skip-Marker,
        None wenn unbekannt.

        Die Skip-Form ist absichtlich ein dict mit Schluessel `skip`: die
        Aufrufer pruefen seit jeher `"skip" in data` auf dem gelesenen JSON.
        Der Payload wird zurueckgegeben wie geschrieben (dict, Liste, ...)."""
        shard = self.shard_of(id)
        ent = self._entries(shard).get(id)
        if ent is None:
            return None
        rec = self._read_record(shard, ent, id)
        if rec is None:
            # Offsets passen nicht mehr (in-place veraenderte Datei) -> einmal
            # komplett neu scannen. Unter dem Shard-Lock, damit ein parallel
            # schreibender Thread nicht auf den verworfenen Index schreibt.
            # Der Sidecar fliegt hier mit raus: er ist die wahrscheinlichste
            # Quelle der falschen Offsets, und ohne ihn zu loeschen laege er
            # beim Neuaufbau sofort wieder auf dem Tisch (der Vollscan schreibt
            # danach einen korrekten).
            with _lock_for(self.path_of(shard)):
                self._index.pop(shard, None)
                try:
                    self.idx_path_of(shard).unlink(missing_ok=True)
                except OSError:
                    pass
                ent = self._entries(shard).get(id)
            if ent is None:
                return None
            rec = self._read_record(shard, ent, id)
            if rec is None:
                return None
        if "skip" in rec:
            return {"skip": rec["skip"]}
        return rec.get("d")

    def timestamp(self, id) -> float | None:
        """`t` der juengsten Zeile (Epoch-Sekunden) oder None."""
        ent = self._entries(self.shard_of(id)).get(id)
        return None if ent is None else ent[2]

    def fresh(self, id, ttl: float = DEFAULT_TTL_S, now: float | None = None) -> bool:
        """True, wenn die id bekannt und juenger als `ttl` ist.

        Semantik von `cacheio.fresh`, nur auf dem Record-`t` statt der
        Datei-mtime."""
        ts = self.timestamp(id)
        if ts is None:
            return False
        now = time.time() if now is None else now
        return (now - ts) < ttl

    def ids(self, include_skip: bool = False) -> set:
        """Alle bekannten ids ueber alle Shards (ohne Skip-Marker per Default)."""
        out: set = set()
        for shard in self.shards():
            for rid, ent in self._snapshot(shard):
                if include_skip or not ent[3]:
                    out.add(rid)
        return out

    def count(self, include_skip: bool = False) -> int:
        """Anzahl bekannter ids (ohne Voll-Parse, nur ueber die Offset-Maps)."""
        total = 0
        for shard in self.shards():
            for _rid, ent in self._snapshot(shard):
                if include_skip or not ent[3]:
                    total += 1
        return total

    def iter(self, include_skip: bool = False, shards=None):
        """Generator ueber (id, payload) - sequentiell, Shard fuer Shard.

        Verdraengte Zeilen (aeltere Version derselben id) werden anhand des
        Offsets aus der Map uebersprungen, kaputte Zeilen tolerant ausgelassen.
        Skip-Records liefern `{"skip": <grund>}` wie `get()`.

        `shards` (optional, Iterable von Shard-Namen) beschraenkt den Durchlauf
        auf diese Shards - unbekannte Namen werden still ignoriert. Fuer Aufrufer,
        die nur EINE Platform brauchen (z. B. der Region-Worker im
        `update`-Dauerlauf): ohne den Filter laese jeder Region-Thread auch die
        (GB-grossen) Shards der Nachbarregionen komplett mit."""
        wanted = None if shards is None else set(shards)
        for shard in self.shards():
            if wanted is not None and shard not in wanted:
                continue
            entries = dict(self._snapshot(shard))
            for start, line, _end in _iter_lines(self.path_of(shard)):
                try:
                    rec = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                rid = rec.get("id")
                ent = entries.get(rid)
                if ent is None or ent[0] != start:
                    continue          # veraltete Zeile derselben id
                if "skip" in rec:
                    if include_skip:
                        yield rid, {"skip": rec["skip"]}
                    continue
                yield rid, rec.get("d")

    # --- Pflege ---

    def __repr__(self) -> str:
        scope = f"{self.kind}/{self.patch}" if self.patch else self.kind
        return f"<ShardStore {scope} @ {self.root}>"

    def compact(self, drop_older_than: float | None = None) -> dict:
        """Alle Shards atomar neu schreiben: Duplikate und kaputte Zeilen raus.

        `drop_older_than` (Epoch-Sekunden) wirft ids ganz raus, deren juengster
        Record aelter ist - so wird aus `purge --mastery-older-than N` ein
        compact-Lauf statt zehntausender Datei-Loeschungen.

        Ein Shard, an dem nichts zu aendern waere, wird NICHT neu geschrieben
        (ein Timeline-Shard ist ~1 GB gross). Rueckgabe: Statistik mit
        `lines` (gelesene Zeilen), `kept`, `dropped` (Duplikate + zu alte) und
        `broken` (unlesbare Zeilen)."""
        stats = {"shards": 0, "lines": 0, "kept": 0, "dropped": 0, "broken": 0}
        for shard in self.shards():
            path = self.path_of(shard)
            with _lock_for(path):
                records: dict = {}   # id -> rohe Zeile, last-wins
                meta: dict = {}      # id -> (t, skip)
                lines = broken = 0
                for _start, line, _end in _iter_lines(path):
                    lines += 1
                    parsed = _parse_full(line)
                    if parsed is None:
                        broken += 1
                        continue
                    rid, t, skip = parsed
                    records[rid] = line
                    meta[rid] = (t, skip)
                if drop_older_than is not None:
                    for rid in [r for r, (t, _s) in meta.items()
                                if t < drop_older_than]:
                        del records[rid]
                kept = len(records)
                stats["shards"] += 1
                stats["lines"] += lines
                stats["kept"] += kept
                stats["broken"] += broken
                stats["dropped"] += lines - broken - kept
                if broken == 0 and kept == lines:
                    continue                     # nichts zu tun, 1 GB gespart
                tmp = path.with_suffix(".tmp")
                entries: dict = {}
                pos = 0
                with tmp.open("wb") as fh:
                    for rid, line in records.items():
                        fh.write(line + b"\n")
                        t, skip = meta[rid]
                        entries[rid] = (pos, len(line), t, skip)
                        pos += len(line) + 1
                # Der alte Sidecar beschreibt die alte Datei - vor dem Replace
                # weg damit, damit selbst ein Absturz zwischen beiden Schritten
                # keinen passenden-aber-falschen Sidecar hinterlaesst.
                try:
                    self.idx_path_of(shard).unlink(missing_ok=True)
                except OSError:
                    pass
                tmp.replace(path)
                # Offsets sind jetzt andere - sie stehen aber schon fest (beim
                # Schreiben mitgezaehlt), ein erneuter Scan waere Verschwendung.
                state = _ShardIndex(entries, pos)
                self._index[shard] = state
                self._persist(shard, state, force=True)
        return stats


class _BulkWriter:
    """Haelt je Shard ein offenes Append-Handle ueber viele Records.

    Zwei bewusste Unterschiede zum Einzel-`put`:

    - **Kein flush je Zeile.** Der Puffer wird erst beim Schliessen (oder wenn
      voll) geschrieben; ein Abbruch mitten im Lauf kann also eine halbe Zeile
      hinterlassen. Genau dafuer sind die toleranten Leser und `compact()` da,
      und der Sammelschreiber laeuft nur in Offline-Laeufen (Migration).
    - **Keine Offset-Pflege waehrend des Laufs.** Bei gepuffertem Schreiben
      taugt `tell()` nicht als Datei-Ende (ein Fremdschreiber koennte dazwischen
      anhaengen), deshalb werden die betroffenen Shard-Indizes beim Schliessen
      verworfen und dort EINMAL neu aufgebaut - inklusive frischem Sidecar,
      damit der Folgeprozess den Vollscan ueber das Geschriebene nicht bezahlt.
    """

    def __init__(self, store: "ShardStore"):
        self._store = store
        self._handles: dict = {}

    def _handle(self, shard: str):
        fh = self._handles.get(shard)
        if fh is None:
            if not self._store._dir_ready:
                self._store.root.mkdir(parents=True, exist_ok=True)
                self._store._dir_ready = True
            fh = self._store.path_of(shard).open("ab")
            self._handles[shard] = fh
        return fh

    def _write(self, id, rec: dict) -> None:
        shard = self._store.shard_of(id)
        data = _dump(rec)
        with _lock_for(self._store.path_of(shard)):
            self._handle(shard).write(data)

    def put(self, id, payload, t: float | None = None) -> None:
        self._write(id, {"id": id, "t": time.time() if t is None else float(t),
                         "d": payload})

    def put_skip(self, id, reason: str, t: float | None = None) -> None:
        self._write(id, {"id": id, "t": time.time() if t is None else float(t),
                         "skip": reason})

    def close(self) -> None:
        store = self._store
        for shard, fh in self._handles.items():
            fh.close()
            with _lock_for(store.path_of(shard)):
                store._index.pop(shard, None)     # Offsets neu einlesen lassen
                # Gleich wieder aufbauen und den Sidecar erneuern: die
                # Massen-Appends (Migration, Backfills) sind genau der Fall, in
                # dem der naechste Prozess sonst den Vollscan bezahlt. Der
                # Aufbau nutzt den bestehenden Sidecar plus Delta - der Anhang
                # ist append-only, die Signatur haelt.
                store._persist(shard, store._state(shard), force=True)
        self._handles.clear()

    def __enter__(self) -> "_BulkWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- Geteilte Instanzen -----------------------------------------------------
# Wer VIELE Einzelabfragen ueber wechselnde Patches macht (App: Post-Game und
# Match-History fragen je Match patch-uebergreifend nach), darf pro Abfrage
# keine neue Instanz bauen - jede haette den Shard erneut gescannt (Timelines:
# ~1 GB je Shard). Diese Instanzen leben deshalb prozessweit weiter; ihre
# Offset-Maps ziehen Neuzugaenge ohnehin per Delta nach (s. ShardStore).

_SHARED: dict[tuple, ShardStore] = {}
_SHARED_GUARD = threading.Lock()


def shared(cfg: Config, kind: str, patch: str | None = None) -> ShardStore:
    """Prozessweit wiederverwendete Store-Instanz je (cache_dir, kind, patch).

    Fuer Lesepfade mit vielen Einzelabfragen. Fetch-Schleifen der Pipeline
    reichen ihre eigene Instanz durch (Lebensdauer = Lauf) und brauchen das
    hier nicht."""
    key = (str(cfg.cache_dir), kind, patch)
    store = _SHARED.get(key)
    if store is None:
        with _SHARED_GUARD:
            store = _SHARED.get(key)
            if store is None:
                store = ShardStore(cfg, kind, patch)
                _SHARED[key] = store
    return store
