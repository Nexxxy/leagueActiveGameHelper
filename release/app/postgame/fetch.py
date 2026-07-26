"""IO-Schicht des Post-Game-Reports: Match + Timeline laden (Cache-first,
sonst RiotClient mit Retry/Backoff), Identitaets-Aufloesung (me:) und die
Item-ID->Name-Abbildung.

Cache-Layout wie der Rest der Pipeline: `data/pipeline/matches/<patch>/<id>.json`
und `timelines/<patch>/<id>.json`. Da der Report mit EINER Match-ID aufgerufen
wird und der Patch vorab nicht bekannt ist, wird zuerst der Cache patch-uebergreifend
durchsucht; erst wenn dort nichts liegt, geht ein API-Call raus.
"""

import time

from core import ddragon
from core.cacheio import read_json, write_json
from core.config import Config
from core.riot_api import RiotClient

# Match-ID-Praefix (Platform) -> Regional-Routing-Host fuer match-v5/account-v1.
_PLATFORM_ROUTING = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "oc1": "americas",
    "kr": "asia", "jp1": "asia",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "ph2": "sea", "sg2": "sea", "th2": "sea", "tw2": "sea", "vn2": "sea",
}


# Summoner's-Rift-5v5-Queues, fuer die der Report ueberhaupt gilt (s.
# plan_postgame.md §7): 420 Ranked Solo/Duo, 440 Ranked Flex, 400 Normal Draft,
# 430 Normal Blind, 490 Quickplay, 480 Swiftplay. Nicht-SR (z. B. ARAM 450)
# wird bei --latest uebersprungen.
SR_5V5_QUEUES = frozenset({400, 420, 430, 440, 480, 490})


def platform_of(match_id: str) -> str:
    """'EUW1_7923765095' -> 'euw1' (Platform-Praefix vor dem ersten '_')."""
    return match_id.split("_", 1)[0].strip().lower()


def routing_of(match_id: str) -> str:
    """Regional-Routing-Host aus der Match-ID ('EUW1_...' -> 'europe').
    Unbekannte Platform -> 'europe' (Default)."""
    return _PLATFORM_ROUTING.get(platform_of(match_id), "europe")


def _find_cached(cfg: Config, kind: str, match_id: str):
    """Sucht `<kind>/<patch>/<id>.json` patch-uebergreifend im Cache.
    Rueckgabe (patch, data) oder (None, None). Skip-Marker gelten als 'nicht da'."""
    base = cfg.cache_dir / kind
    if not base.exists():
        return None, None
    for patch_dir in sorted(base.iterdir(), reverse=True):
        path = patch_dir / f"{match_id}.json"
        if path.exists():
            data = read_json(path)
            if isinstance(data, dict) and "skip" in data:
                continue
            return patch_dir.name, data
    return None, None


def _make_client(cfg: Config, keys, platform: str, routing: str) -> RiotClient:
    """RiotClient aus einer KONKRETEN Key-Liste (Low-Level-Seam).

    Getrennt von der Key-Auswahl, damit `_FallbackClient` denselben Bauweg fuer
    Primary- und Fallback-Keys nutzt und Tests die Client-Erzeugung mocken
    koennen, ohne die Vorrang-Logik zu umgehen."""
    return RiotClient(keys, platform, routing,
                      cfg.rate_limit_per_sec, cfg.rate_limit_per_2min)


class _FallbackClient:
    """RiotClient-Proxy fuer Postgame-Fetches: strikter Dev-Key-Vorrang mit
    einmaligem Fallback auf den `api_key`.

    Postgame-Fetches (Match/Timeline, --latest, Enrichment) bevorzugen den
    `dev_api_key`, damit der Haupt-`api_key` fuer parallel laufende
    Pipeline-Crawls frei bleibt (s. Config.postgame_keys / plan_postgame.md
    §2.2b). Der Proxy baut den RiotClient zunaechst NUR mit den Primary-Keys (bei
    gesetztem Dev-Key also dev-only). Lehnt Riot alle aktiven Keys ab (RiotClient
    wirft dann SystemExit), wird EINMALIG mit den Fallback-Keys (api_key) neu
    gebaut und der fehlgeschlagene Aufruf wiederholt - mit einer Log-Zeile.

    Bewusst KEIN Round-Robin ueber beide Keys im Vorrang-Modus: RiotClient
    verteilt Requests sonst gleichmaessig auf alle aktiven Keys und wuerde damit
    den Haupt-Key mitbelasten. Der Vorrang ist strikt: erst dev-only, bei
    Ablehnung api-only. Ohne Fallback-Keys (kein Dev-Vorrang oder
    `round_robin: true`) verhaelt sich der Proxy wie der nackte RiotClient - dann
    buendelt RiotClient beide Keys wie bisher (round_robin bleibt unberuehrt).

    Nur von RiotClient-Methodenaufrufen ausgeloeste SystemExit ('Alle API-Keys
    abgelehnt') triggert den Fallback; die SystemExit der fetch-Logik selbst
    (z. B. 'Match nicht abrufbar') laufen ausserhalb der proxierten Aufrufe und
    bleiben unberuehrt."""

    def __init__(self, make, primary_keys, fallback_keys, log=print):
        self._make = make                         # keys(tuple) -> RiotClient
        self._fallback_keys = tuple(fallback_keys)
        self._log = log
        self._client = make(primary_keys)
        # Kein Fallback moeglich/noetig -> Proxy ist schon "durchgereicht".
        self._exhausted = not self._fallback_keys

    def _fall_back(self) -> None:
        self._log("[postgame] dev_api_key abgelehnt - Fallback auf api_key")
        self._client = self._make(self._fallback_keys)
        self._exhausted = True

    def __getattr__(self, name):
        # Wird nur fuer Attribute aufgerufen, die der Proxy selbst nicht hat
        # (die eigenen _-Attribute liegen im __dict__ und triggern das nicht).
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except SystemExit:
                if self._exhausted:
                    raise               # kein Fallback (mehr) -> durchreichen
                self._fall_back()
                return getattr(self._client, name)(*args, **kwargs)

        return wrapped


def _build_client(cfg: Config, match_id: str) -> _FallbackClient:
    """Postgame-Client mit korrektem Routing fuer die Match-ID (unabhaengig vom
    konfigurierten Default-Routing - die ID bestimmt die Region). Dev-Key-Vorrang
    + Fallback via `_FallbackClient` (s. Config.postgame_keys)."""
    routing = routing_of(match_id)
    platform = platform_of(match_id)
    primary, fallback = cfg.postgame_keys
    return _FallbackClient(
        lambda keys: _make_client(cfg, keys, platform, routing),
        primary, fallback)


def _region_client(cfg: Config) -> _FallbackClient:
    """Postgame-Client fuer die KONFIGURIERTE Heimatregion (cfg.platform/
    cfg.routing), mit Dev-Key-Vorrang + Fallback.

    Fuer --latest/Enrichment gibt es (anders als beim expliziten Aufruf mit
    Match-ID) noch keine ID, aus der sich die Region ableiten liesse - die
    eigenen Matches liegen in der eigenen Region. Eigener Seam, damit Tests ihn
    mocken koennen."""
    primary, fallback = cfg.postgame_keys
    return _FallbackClient(
        lambda keys: _make_client(cfg, keys, cfg.platform, cfg.routing),
        primary, fallback)


def _fetch_with_retry(call, retries: int, backoff: float, log):
    """Ruft `call()` (RiotClient-Endpoint, None bei 404) mit Retry/Backoff.

    Fuer den Fall 'Match direkt nach Spielende noch nicht abrufbar' (Phase-2-
    Vorbereitung): bei None wird bis `retries`-mal mit wachsendem Backoff erneut
    versucht. Bei einer vergangenen ID liegt das Ergebnis sofort vor."""
    for attempt in range(retries + 1):
        data = call()
        if data is not None:
            return data
        if attempt < retries:
            wait = backoff * (attempt + 1)
            log(f"[postgame] noch nicht verfuegbar - Retry in {wait:.0f}s "
                f"({attempt + 1}/{retries})")
            time.sleep(wait)
    return None


def load_match_and_timeline(cfg: Config, match_id: str, *, retries: int = 0,
                            backoff: float = 15.0, log=print):
    """(patch, match, timeline). Cache-first, sonst API (mit Retry/Backoff).

    Frisch gezogene Daten werden unter dem aus `gameVersion` abgeleiteten Patch
    gecacht, damit ein zweiter Report-Lauf offline bleibt. Wirft SystemExit,
    wenn das Match dauerhaft nicht auffindbar ist."""
    patch, match = _find_cached(cfg, "matches", match_id)
    _, timeline = _find_cached(cfg, "timelines", match_id) if patch else (None, None)
    client = None

    if match is None:
        client = _build_client(cfg, match_id)
        log(f"[postgame] Match {match_id} nicht im Cache - lade ueber API "
            f"({client.routing}) ...")
        match = _fetch_with_retry(lambda: client.match(match_id),
                                  retries, backoff, log)
        if match is None:
            raise SystemExit(f"Match {match_id} nicht abrufbar (404/Timeout).")
        patch = ddragon.patch_of(match["info"].get("gameVersion", ""))
        write_json(cfg.cache_dir / "matches" / patch / f"{match_id}.json", match)

    if timeline is None:
        if client is None:
            client = _build_client(cfg, match_id)
        log(f"[postgame] Timeline {match_id} nicht im Cache - lade ueber API ...")
        timeline = _fetch_with_retry(lambda: client.match_timeline(match_id),
                                     retries, backoff, log)
        if timeline is None:
            raise SystemExit(f"Timeline zu {match_id} nicht abrufbar.")
        write_json(cfg.cache_dir / "timelines" / patch / f"{match_id}.json",
                   timeline)

    return patch, match, timeline


# --- Neuestes Spiel aufloesen (--latest) ------------------------------------

def _resolve_puuid(client: RiotClient, ident: str, log=print):
    """Identitaet (Riot-ID 'Name#Tag' ODER PUUID) -> PUUID.

    Ohne '#' gilt `ident` bereits als PUUID (unveraendert zurueck). Eine Riot-ID
    wird ueber account-v1 aufgeloest. Rueckgabe None, wenn die Riot-ID unbekannt
    ist (404)."""
    if "#" not in ident:
        return ident
    name, _, tag = ident.partition("#")
    acc = client.account_by_riot_id(name.strip(), tag.strip())
    if acc and acc.get("puuid"):
        return acc["puuid"]
    log(f"[postgame] Riot-ID '{ident}' nicht gefunden (account-v1).")
    return None


def _queue_id_of(cfg: Config, client: RiotClient, match_id: str, log=print):
    """queueId eines Matches, Cache-first (patch-uebergreifend), sonst API.

    Ein per API geholtes Match wird gleich in den Cache geschrieben, damit der
    nachfolgende `run()`/`load_match_and_timeline` fuer das gewaehlte Spiel
    offline bleibt (kein zweiter match-Call). Rueckgabe None, wenn das Match
    nicht abrufbar ist."""
    _, cached = _find_cached(cfg, "matches", match_id)
    if cached is not None:
        return cached.get("info", {}).get("queueId")
    match = client.match(match_id)
    if match is None:
        log(f"[postgame] Match {match_id} nicht abrufbar - uebersprungen.")
        return None
    patch = ddragon.patch_of(match["info"].get("gameVersion", ""))
    write_json(cfg.cache_dir / "matches" / patch / f"{match_id}.json", match)
    return match["info"].get("queueId")


def resolve_latest_match_id(cfg: Config, *, me: str | None = None,
                            lookback: int = 5, log=print) -> str:
    """Neueste SR-5v5-Match-ID des Nutzers auflaufen (fuer `postgame --latest`).

    Ablauf: Identitaet (`me`/PUUID/config `me:`) -> PUUID -> die neuesten
    `lookback` Match-IDs (Match-v5, OHNE queue-/type-Filter, also ranked UND
    normal) -> die neueste ID nehmen, deren queueId eine SR-5v5-Queue ist
    (Nicht-SR wie ARAM wird uebersprungen). Wirft SystemExit mit klarer Meldung,
    wenn Identitaet fehlt/unaufloesbar ist oder keine der letzten IDs SR-5v5 ist."""
    ident = (me or cfg.me or "").strip()
    if not ident:
        raise SystemExit(
            "[postgame] --latest braucht eine Identitaet: --me 'Name#Tag', "
            "--puuid <PUUID> oder 'me:' in config.yml setzen.")

    client = _region_client(cfg)
    puuid = _resolve_puuid(client, ident, log=log)
    if not puuid:
        raise SystemExit(
            f"[postgame] Identitaet '{ident}' nicht aufloesbar - Riot-ID pruefen "
            f"(Name#Tag) oder direkt --puuid angeben.")

    log(f"[postgame] Suche neuestes SR-5v5-Spiel fuer '{ident}' "
        f"({cfg.routing}) ...")
    ids = client.match_ids(puuid, queue=None, count=lookback,
                           type_filter=None) or []
    if not ids:
        raise SystemExit(
            f"[postgame] Keine Matches fuer '{ident}' gefunden "
            f"(Region {cfg.routing}?).")

    for mid in ids:
        qid = _queue_id_of(cfg, client, mid, log=log)
        if qid in SR_5V5_QUEUES:
            log(f"[postgame] Neuestes SR-5v5-Spiel: {mid} (Queue {qid}).")
            return mid

    raise SystemExit(
        f"[postgame] Keins der letzten {len(ids)} Spiele ist Summoner's Rift "
        f"5v5 - nur SR wird ausgewertet (ARAM u. a. werden uebersprungen).")


def resolve_match_after(cfg: Config, baseline: str | None, *, me: str | None = None,
                        lookback: int = 5, retries: int = 8, backoff: float = 10.0,
                        log=print) -> str | None:
    """Neue SR-5v5-Match-ID nach Spielende aufloesen (fuer den Auto-Trigger).

    Match-V5 indexiert ein Match erst Sekunden bis Minuten nach Spielende. Damit
    der Auto-Report das GERADE beendete Spiel trifft (und nicht das vorige), wird
    `resolve_latest_match_id` mit Retry/Backoff (dieselbe `_fetch_with_retry`-
    Mechanik wie beim Match-/Timeline-Fetch) so lange gepollt, bis die neueste ID
    != `baseline` ist (= das neue Spiel ist indexiert). `baseline` ist die vor
    Spielbeginn gemerkte neueste ID; ist sie None (Baseline-Aufloesung schlug
    fehl), gilt die erste erfolgreich aufgeloeste ID.

    Rueckgabe die neue ID oder None, wenn das Retry-Budget erschoepft ist (dann
    sauber aufgeben - der Aufrufer loggt und bricht ab)."""
    def _call():
        try:
            mid = resolve_latest_match_id(cfg, me=me, lookback=lookback, log=log)
        except SystemExit as exc:
            # Noch kein (SR-)Match abrufbar -> wie 'nicht verfuegbar' behandeln.
            log(f"[postgame] {exc}")
            return None
        if baseline is not None and mid == baseline:
            return None   # noch das alte Spiel -> weiter warten
        return mid

    return _fetch_with_retry(_call, retries, backoff, log)


# --- Identitaet (me:) -------------------------------------------------------

def resolve_me_pid(cfg: Config, match: dict, me: str | None, log=print):
    """Ermittelt die participantId des eigenen Spielers im Match.

    `me` kann eine PUUID (kein '#') oder eine Riot-ID 'Name#Tag' sein; None ->
    cfg.me. Eine Riot-ID wird zuerst gegen die riotId-Felder der Participants
    geprueft (offline, kein API-Call); erst bei Fehlschlag ueber account-v1 zur
    PUUID aufgeloest. Rueckgabe pid (int) oder None, wenn nicht bestimmbar."""
    ident = (me or cfg.me or "").strip()
    parts = match["info"]["participants"]
    if not ident:
        return None

    if "#" in ident:
        name, _, tag = ident.partition("#")
        name_l, tag_l = name.strip().lower(), tag.strip().lower()
        for p in parts:
            if (str(p.get("riotIdGameName", "")).lower() == name_l
                    and str(p.get("riotIdTagline", "")).lower() == tag_l):
                return p.get("participantId")
        # Fallback: account-v1 -> PUUID -> Participant.
        client = _build_client(cfg, match["metadata"]["matchId"])
        acc = client.account_by_riot_id(name.strip(), tag.strip())
        if acc and acc.get("puuid"):
            return _pid_by_puuid(parts, acc["puuid"])
        log(f"[postgame] Riot-ID '{ident}' nicht im Match gefunden.")
        return None

    # sonst PUUID
    return _pid_by_puuid(parts, ident)


def _pid_by_puuid(parts: list, puuid: str):
    for p in parts:
        if p.get("puuid") == puuid:
            return p.get("participantId")
    return None


# --- Static: Item-Namen -----------------------------------------------------

def _item_static(cfg: Config, patch: str) -> dict:
    """Static-Item-Daten fuer den Patch (Cache-first, sonst ddragon).

    Gemeinsame Quelle fuer `item_name_lookup` (ID->Name) und `item_gold_lookup`
    (ID->gold.total). Nutzt `data/pipeline/static/item_<patch>*.json`; fehlt der
    Cache, wird ueber ddragon nachgeladen (patch->passende Version)."""
    static = cfg.cache_dir / "static"
    cached = sorted(static.glob(f"item_{patch}*.json")) if static.exists() else []
    if cached:
        return read_json(cached[-1])
    version = _version_for_patch(patch)
    return ddragon.items(version, cfg.cache_dir)


def item_name_lookup(cfg: Config, patch: str):
    """Callable itemId(int) -> Item-Name(str)|None fuer den Patch."""
    data = _item_static(cfg, patch)
    names = {int(i): v.get("name") for i, v in data.get("data", {}).items()}

    def lookup(iid):
        return names.get(int(iid)) if iid else None

    return lookup


def item_gold_lookup(cfg: Config, patch: str):
    """Callable itemId(int) -> gold.total(int) fuer den Patch (0 bei unbekannt).

    Parallel zu `item_name_lookup`: liefert den vollen Item-Goldwert
    (`gold.total`) aus dem Static-Cache - Basis der einheitlichen Gold-Metrik
    (gehaltenes Item-Gold) im Timeline-Pfad (s. series.build_series)."""
    data = _item_static(cfg, patch)
    gold = {int(i): ((v.get("gold", {}) or {}).get("total", 0) or 0)
            for i, v in data.get("data", {}).items()}

    def lookup(iid):
        return gold.get(int(iid), 0) if iid else 0

    return lookup


# --- Static: Antiheal-Items (Grievous Wounds) -------------------------------
#
# Antiheal wird NICHT ueber eine gepflegte ID-Liste erkannt, sondern ueber die
# Static-Item-Beschreibung - Riot benennt und nummeriert Items um, der Effekt-
# Text bleibt. Marker sind "grievous" (klassisch "Grievous Wounds") UND
# "wounds": seit dem 2026er-Item-Rework tragen mehrere Items (Thornmail,
# Bramble Vest, Chempunk Chainsword) nur noch "<keyword>40% Wounds</keyword>"
# im Text. Am 16.14-Static trifft der Marker exakt die acht bekannten
# SR-Antiheal-Items (plus deren Arena-Varianten, die auf SR nie auftauchen).
ANTIHEAL_MARKERS = ("grievous", "wounds")

# Fallback NUR fuer den Fall, dass die Static-Daten fehlen oder der Marker ins
# Leere laeuft (Locale-Wechsel, kaputter Cache): die bekannten Antiheal-IDs
# Executioner's Calling / Mortal Reminder / Bramble Vest / Thornmail /
# Oblivion Orb / Morellonomicon / Chemtech Putrifier / Chempunk Chainsword.
ANTIHEAL_FALLBACK_IDS = frozenset({3123, 3033, 3076, 3075, 3916, 3165, 3011,
                                   6609})


def antiheal_item_ids(cfg: Config, patch: str) -> frozenset:
    """Item-IDs mit Grievous-Wounds-/Anti-Heal-Effekt fuer den Patch.

    Liest die Static-Item-Daten und nimmt jedes Item, dessen `description`
    (bzw. `plaintext`) einen ANTIHEAL_MARKERS-Marker enthaelt. Greift das nicht
    (kein Static, kein Treffer), gilt ANTIHEAL_FALLBACK_IDS - lieber die
    bekannten IDs als gar keine Erkennung."""
    try:
        data = _item_static(cfg, patch)
    except Exception:   # noqa: BLE001 - Antiheal-Befund ist optional
        return ANTIHEAL_FALLBACK_IDS
    ids = set()
    for iid, item in (data.get("data", {}) or {}).items():
        text = ((item.get("description") or "")
                + " " + (item.get("plaintext") or "")).lower()
        if any(m in text for m in ANTIHEAL_MARKERS):
            try:
                ids.add(int(iid))
            except (TypeError, ValueError):
                continue
    return frozenset(ids) if ids else ANTIHEAL_FALLBACK_IDS


def _version_for_patch(patch: str) -> str:
    """Volle Data-Dragon-Version zu einem Patch ('16.14' -> '16.14.1').
    Sucht in der Versionsliste die erste passende, sonst latest."""
    try:
        import requests
        resp = requests.get(f"{ddragon.BASE}/api/versions.json", timeout=15)
        resp.raise_for_status()
        for v in resp.json():
            if ddragon.patch_of(v) == patch:
                return v
    except Exception:   # noqa: BLE001 - offline/kaputt -> latest
        pass
    return ddragon.latest_version()
