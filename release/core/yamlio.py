"""Schnelles YAML-Laden mit Wiederholungs-Cache.

Hintergrund: die generierte `builds.yaml` ist mehrere MB gross und wurde ueber
`yaml.safe_load` mit dem REINEN Python-Parser gelesen - rund 10 s pro Aufruf.
Gelesen wird sie aber mehrfach je Prozess (Wissensbasis, Core-Sets des
Post-Game-Reports, Vererbung, Focus-Report), und in der Testsuite zusaetzlich
einmal pro parallelem Worker. Das war der mit Abstand groesste Zeitfresser.

Zwei Hebel, beide ohne Verhaltensaenderung:

1. **libyaml statt Python-Parser** (`yaml.CSafeLoader`): dieselbe Semantik wie
   `yaml.safe_load`, nur in C - Faktor ~4,5. Fehlt die C-Erweiterung, faellt der
   Loader automatisch auf `yaml.SafeLoader` zurueck.
2. **Wiederholungs-Cache je Datei-INHALT**: der zweite Aufruf auf denselben
   Inhalt parst nicht erneut, sondern entpackt eine gepickelte Kopie - Faktor
   ~100 gegenueber dem C-Parser.

Der Cache-Schluessel ist bewusst ein Hash des Inhalts und NICHT (mtime, Groesse):
Windows loest Datei-mtimes nur grob auf (gemessen: von 200 schnell
aufeinanderfolgenden Schreibvorgaengen teilten sich 171 denselben
`st_mtime_ns`). Ein Zeitstempel-Schluessel wuerde eine frisch ueberschriebene
Datei gleicher Groesse still als "unveraendert" durchwinken - genau das Muster,
das Tests erzeugen, die eine builds.yaml mehrfach in dasselbe tmp-Verzeichnis
schreiben. Datei lesen + hashen kostet ~10 ms gegen ~2 100 ms Parsen; die
Korrektheit ist den Bruchteil wert.

Wichtig fuer die Gleichheit zum bisherigen Verhalten: jeder Aufruf liefert ein
FRISCHES, privates Objekt (der Cache haelt die Pickle-BYTES, nicht das Objekt).
Aufrufer duerfen ihr Ergebnis also weiterhin gefahrlos mutieren - genau das tut
z. B. `engine.knowledge.load()` beim Einmischen der Overrides. Ein geteiltes
Objekt haette dort still ueber Aufrufgrenzen hinweg durchgeschlagen.
"""

import hashlib
import os
import pickle
from pathlib import Path

import yaml

# libyaml, wenn vorhanden - sonst der reine Python-Parser. Beide erzeugen fuer
# `safe`-YAML dieselben Objekte (nur str/int/float/bool/None/list/dict).
SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Wie viele verschiedene Inhalte gleichzeitig im Cache liegen duerfen. Klein
# gehalten: die grossen builds.yaml belegen als Pickle je ~4,5 MB, und in der
# Praxis rotiert hoechstens zwischen aktuellem Patch, Vorgaenger-Patch
# (Vererbung) und einer gepinnten Testquelle.
_CACHE_MAX = 4

# blake2b-Digest des Datei-Inhalts -> pickle-Bytes des geparsten Inhalts.
_CACHE: dict[bytes, bytes] = {}

# --- Sidecar (prozessuebergreifender Cache) ---------------------------------
# Der Speicher-Cache hilft nur INNERHALB eines Prozesses. Server-Start und
# Testsuite starten aber staendig neue Prozesse - beim parallelen Testlauf sogar
# ein Dutzend gleichzeitig, die alle dieselbe builds.yaml parsen. Neben grosse
# Quelldateien legen wir daher das geparste Ergebnis als Sidecar ab; der
# naechste Prozess entpackt es (~0,07 s) statt neu zu parsen (~2,1 s).
#
# Erste 16 Byte des Sidecars sind der Inhalts-Hash der Quelldatei. Passt er
# nicht, wird das Sidecar ignoriert (und beim naechsten Schreiben ersetzt) -
# eine veraltete Datei kann also nie ein falsches Ergebnis liefern, und es wird
# nur entpackt, was nachweislich zur aktuellen Quelle gehoert.
_SIDECAR_SUFFIX = ".parsecache"

# Erst ab dieser Groesse lohnt das Sidecar. Kleine YAMLs (config.yml,
# overrides.yaml, Test-Fixtures) parsen in Millisekunden - fuer die waere die
# Zusatzdatei reine Unordnung.
_SIDECAR_MIN_BYTES = 1_000_000


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + _SIDECAR_SUFFIX)


def _read_sidecar(path: Path, key: bytes) -> bytes | None:
    """Pickle-Bytes aus dem Sidecar, wenn es zur aktuellen Quelle passt."""
    try:
        blob = _sidecar_path(path).read_bytes()
    except OSError:
        return None
    return blob[16:] if len(blob) > 16 and blob[:16] == key else None


def _write_sidecar(path: Path, key: bytes, blob: bytes) -> None:
    """Sidecar atomar schreiben - best effort.

    Ueber `os.replace` (atomar), damit parallele Prozesse sich nie eine halb
    geschriebene Datei zeigen. Jeder Fehler wird geschluckt: ein Cache, der sich
    nicht anlegen laesst (Nur-Lese-Verzeichnis, volle Platte), darf das Lesen
    der Quelldatei niemals scheitern lassen."""
    side = _sidecar_path(path)
    tmp = side.with_name(f"{side.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(key + blob)
        os.replace(tmp, side)
    except Exception:   # noqa: BLE001 - Cache ist Kuer, nie Pflicht
        try:
            tmp.unlink()
        except OSError:
            pass


def _remember(key: bytes, blob: bytes) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        # Aeltesten Eintrag verdraengen (dict haelt die Einfuegereihenfolge).
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = blob


def load(path: Path):
    """`path` als YAML lesen - Ersatz fuer
    `yaml.safe_load(path.read_text(encoding="utf-8"))`.

    Gleiche Rueckgabe wie bisher (leere Datei -> None); die `or {}`-Absicherung
    bleibt Sache der Aufrufer, damit sich an deren Semantik nichts aendert."""
    path = Path(path)
    raw = path.read_bytes()
    key = hashlib.blake2b(raw, digest_size=16).digest()
    big = len(raw) >= _SIDECAR_MIN_BYTES

    blob = _CACHE.get(key)
    if blob is None and big:
        blob = _read_sidecar(path, key)
        if blob is not None:
            _remember(key, blob)
    if blob is not None:
        try:
            return pickle.loads(blob)
        except Exception:   # noqa: BLE001 - beschaedigter Cache: frisch parsen
            _CACHE.pop(key, None)

    data = yaml.load(raw.decode("utf-8"), Loader=SafeLoader)
    try:
        blob = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:   # noqa: BLE001 - exotischer Inhalt: dann eben ohne Cache
        return data
    _remember(key, blob)
    if big:
        _write_sidecar(path, key, blob)
    return data


def clear_cache() -> None:
    """Speicher-Cache leeren (Diagnose/Tests). Sidecars bleiben liegen - sie
    sind ueber den Inhalts-Hash selbst-invalidierend."""
    _CACHE.clear()
