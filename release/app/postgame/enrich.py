"""Zuordnung eines Live-Mitschnitts zur ECHTEN Match-ID (Stufe 2, Phase 3).

Der Live-Client-Mitschnitt (Port 2999) kennt seine Match-ID nicht. Sobald Riot
das Spiel indexiert hat, findet `find_match_id` es ueber **Identitaet -> PUUID
-> die letzten Match-IDs -> Roster-Vergleich** wieder. Der Aufrufer baut damit
den VOLLEN Match-V5-Report (`postgame.build_report`) und ersetzt den key-freien
Stufe-1-Report komplett.

**WARUM voller Neubau statt Teil-Merge** (Entscheidung 2026-07-30): Bis dahin
wurden nur die Schaden-Serien aus der Timeline in die Live-Serien gemergt, alles
uebrige (Gold/CS/Level/Items/Events) blieb aus der Live-API. Die liefert aber
zeitweise falsche Werte - real gesehen bei Viego (Besessenheits-Mechanik): leere
Item-Listen, dadurch fiel die Item-Gold-Kurve mehrfach auf 0 und das Scoreboard
zeigte 3.800 Item-Gold, waehrend Match-V5 (EUW1_7933910870) 14.200 goldSpent und
einen vollen 6-Item-Build auswies. Match-V5 ist die verlaessliche Quelle - sobald
sie da ist, gilt sie fuer den GANZEN Report. Die Live-Daten bleiben reine
Stufe-1-/Fallback-Quelle (kein Key, Match nicht indexiert, kein Roster-Treffer).

**Jeder Fehlerpfad (kein Key, keine Identitaet, keine IDs, kein Roster-Treffer)
endet in `None`** - der Aufrufer bleibt dann sauber key-frei (Disclaimer, kein
Crash). Netz nur beim Fetch (Cache-first); der RiotClient kommt ueber den Seam
`_region_client`, damit Tests ihn ohne Netz mocken koennen.
"""

from core import ddragon
from core.config import Config
from engine import champions
from . import fetch


def _region_client(cfg: Config):
    """RiotClient der Heimatregion (eigener Seam, in Tests gemockt).

    Das mitgeschnittene Spiel liegt in der eigenen Region des Nutzers - dieselbe
    wie fuer `--latest`. Delegiert an `fetch._region_client`."""
    return fetch._region_client(cfg)


def _load_match(cfg: Config, client, match_id: str):
    """Match Cache-first laden, sonst ueber den Client (und cachen). None bei 404.

    Cache-Layout wie der Rest der Pipeline (Shard-Store, Kind 'matches'); ein
    frisch geholtes Match wird gleich abgelegt, damit der nachfolgende
    `build_report` es ohne zweiten API-Call findet."""
    _, cached = fetch._find_cached(cfg, "matches", match_id)
    if cached is not None:
        return cached
    match = client.match(match_id)
    if match is None:
        return None
    patch = ddragon.patch_of(match["info"].get("gameVersion", ""))
    fetch._cache(cfg, "matches", patch, match_id, match)
    return match


def _canon_champ(name):
    """Champion-Name -> Data-Dragon-ID ('Tahm Kench' -> 'TahmKench').

    WARUM (Bugfix 2026-07-30): Das Live-Capture speichert die ANZEIGENAMEN der
    Live-Client-API ('Nunu & Willump', 'Wukong', 'Bel'Veth'), Match-v5 liefert
    dagegen ddragon-IDs ('Nunu', 'MonkeyKing', 'Belveth'). Ein roher
    Mengenvergleich der Roster scheiterte damit an JEDEM Spiel mit so einem
    Champion - Anreicherung und History-Retry fanden das Match nie.

    Unbekannter Name oder Resolver nicht verfuegbar (kein ddragon-Cache, Netz
    aus, Import-Fehler) -> Originalname. Robustheit vor Vollstaendigkeit: der
    Abgleich darf nie crashen, im schlechtesten Fall vergleicht er wie
    frueher roh."""
    if not name:
        return name
    try:
        return champions.resolve_id(name) or name
    except Exception:   # noqa: BLE001 - Namensaufloesung ist Kuer, kein Muss
        return name


def _canon_champs(names) -> frozenset:
    """Champion-Namen kanonisiert als Menge (leere Namen fallen raus)."""
    return frozenset(_canon_champ(n) for n in (names or []) if n)


def _roster_matches(dump_champs, match: dict) -> bool:
    """True, wenn die Champion-Menge des Matches exakt der des Dumps entspricht.

    Robuster Zuordnungs-Test (statt naiv 'neuestes Spiel'): ein Dump kann ein
    aelteres Spiel sein - nur bei identischer 10er-Champion-Menge ist es sicher
    dasselbe Match (Champions sind pro SR-Spiel eindeutig).

    BEIDE Seiten laufen vorher durch `_canon_champ`, weil Dump (Anzeigename) und
    Match-v5 (ddragon-ID) dieselben Champions verschieden schreiben."""
    parts = (match.get("info", {}) or {}).get("participants", []) or []
    match_champs = _canon_champs(p.get("championName", "") for p in parts)
    dump = _canon_champs(dump_champs)
    return bool(dump) and match_champs == dump


def find_match_id(cfg: Config, pid_map: dict, ident: str | None, *,
                  lookback: int = 10, log=print) -> str | None:
    """Echte Match-ID zum Live-Mitschnitt suchen (oder None).

    Ablauf: Identitaet -> PUUID (account-v1) -> die letzten `lookback` Match-IDs
    (ungefiltert, ranked UND normal) -> je Kandidat das Match Cache-first laden
    und den mit **identischer Champion-Menge** waehlen (Roster-Match, s.
    `_roster_matches`).

    Bei JEDEM Fehlerpfad (kein Key, keine Identitaet, keine IDs, kein
    Roster-Treffer) -> `None`; der Aufrufer bleibt dann key-frei."""
    if not cfg.active_api_keys:
        log("[postgame] Kein API-Key - Report bleibt key-frei (kein Match).")
        return None
    ident = (ident or "").strip()
    if not ident:
        log("[postgame] Keine Identitaet fuer die Match-Zuordnung - key-frei.")
        return None

    client = _region_client(cfg)
    puuid = fetch._resolve_puuid(client, ident, log=log)
    if not puuid:
        log(f"[postgame] Identitaet '{ident}' nicht aufloesbar - key-frei.")
        return None

    ids = client.match_ids(puuid, queue=None, count=lookback,
                           type_filter=None) or []
    if not ids:
        log("[postgame] Keine Match-IDs fuer die Zuordnung gefunden - key-frei.")
        return None

    champs = frozenset(p["champ"] for p in pid_map["parts"] if p["champ"])
    for mid in ids:
        cand = _load_match(cfg, client, mid)
        if cand is not None and _roster_matches(champs, cand):
            log(f"[postgame] Mitschnitt zugeordnet: Match {mid} (Roster-Treffer).")
            return mid
    log("[postgame] Kein Match mit passendem Roster gefunden - key-frei.")
    return None
