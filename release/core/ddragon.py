"""Zugriff auf Data Dragon (statische Spieldaten, kein API-Key noetig)."""

import json
from functools import lru_cache
from pathlib import Path

import requests

BASE = "https://ddragon.leagueoflegends.com"


def latest_version() -> str:
    resp = requests.get(f"{BASE}/api/versions.json", timeout=15)
    resp.raise_for_status()
    return resp.json()[0]


def _version_key(version: str) -> list[int]:
    """Numerischer Sortierschluessel. Lexikografisch waere '16.9.1' > '16.14.1' -
    genau der Fehler, der beim Patchwechsel die falsche Static-Version zieht."""
    return [int(x) for x in version.split(".") if x.isdigit()]


def cached_versions(cache_dir: Path) -> list[str]:
    """Alle VOLLSTAENDIG gecachten Static-Versionen (item_<ver>.json UND
    champion_<ver>.json liegen vor), numerisch aufsteigend sortiert.

    Bewusst nur Paare: die beiden Dateien koennen auseinanderlaufen (ein
    abgebrochener Lauf laedt item_ und champion_ nicht zwingend zusammen); eine
    halbe Version taugt nicht als Fallback-Basis.
    """
    static = Path(cache_dir) / "static"
    if not static.is_dir():
        return []
    found = []
    for path in static.glob("item_*.json"):
        version = path.name[len("item_"):-len(".json")]
        if _version_key(version) and (static / f"champion_{version}.json").exists():
            found.append(version)
    return sorted(found, key=_version_key)


def latest_version_cached(cache_dir: Path) -> str:
    """Wie `latest_version()`, faellt aber ohne Netz auf die neueste vollstaendig
    gecachte Static-Version zurueck.

    Online ist das Verhalten UNVERAENDERT: die Riot-Antwort gewinnt immer, eine
    neue Version laedt `_fetch_cached` automatisch nach (Auto-Refresh des
    keyless-Teils). Der Fallback greift nur, wenn die versions.json nicht
    erreichbar ist - dann startet die App mit den Referenzdaten, die sie hat,
    statt gar nicht.
    """
    try:
        return latest_version()
    except (requests.RequestException, OSError):
        pass
    cached = cached_versions(cache_dir)
    if cached:
        return cached[-1]
    raise RuntimeError(
        f"Data-Dragon-Version nicht ermittelbar: kein Netz und kein "
        f"vollstaendiger Static-Cache in {Path(cache_dir) / 'static'} "
        f"(item_<ver>.json UND champion_<ver>.json derselben Version noetig). "
        f"Einmal MIT Internetverbindung 'python -m pipeline update' oder "
        f"'python -m pipeline focus' laufen lassen.")


def patch_of(version: str) -> str:
    """'26.13.1' -> '26.13' (entspricht dem Prefix von gameVersion in Match-V5)."""
    return ".".join(version.split(".")[:2])


def _fetch_cached(kind: str, version: str, cache_dir: Path) -> dict:
    cache = cache_dir / "static" / f"{kind}_{version}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    resp = requests.get(f"{BASE}/cdn/{version}/data/en_US/{kind}.json", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def items(version: str, cache_dir: Path) -> dict:
    return _fetch_cached("item", version, cache_dir)


def champions(version: str, cache_dir: Path) -> dict:
    return _fetch_cached("champion", version, cache_dir)


# Summoner's-Rift-Filter der Champion-Statik (Scope: Ranked/Normal Draft, SR 5v5).
#
# WARUM: Wie bei den Items (siehe engine/items.py) fuehrt Data Dragon seit
# 16.15.1 MODUS-VARIANTEN mit eigener ID und IDENTISCHEM Anzeigenamen - 60
# 'Jade_*'-Eintraege, z.B. id='Jade_Ashe', key='60022', name='Ashe' neben dem
# echten id='Ashe', key='22'. Weil `_name_lookup` ueber den Anzeigenamen
# indiziert und die Variante in der Dict-Reihenfolge NACH dem Original kommt,
# loeste `resolve_name('Ashe')` zu 'Jade_Ashe' auf; die Wissensbasis wird aber
# unter der echten DD-ID gefuehrt -> `recommend()` fand fuer jeden Champion mit
# Jade-Variante gar nichts mehr.
#
# Zwei Kriterien, beide noetig (Doppelabsicherung wie beim Item-Filter):
# 1. '_' in der ID - echte Data-Dragon-Champion-IDs enthalten nie einen
#    Unterstrich (darauf verlaesst sich auch profiling.verified_champion_id
#    beim Token-Split von rawChampionName).
# 2. key >= 10000 - echte Champion-Keys sind 1- bis 4-stellig, die Varianten
#    haengen ein Modus-Praefix davor (60022 = 60 + 22).
_MODE_VARIANT_KEY = 10000


def _is_sr_champion(info: dict) -> bool:
    try:
        key = int(info.get("key", 0))
    except (TypeError, ValueError):
        # Nicht-numerischer key: kein bekanntes Varianten-Muster -> behalten,
        # der '_'-Test entscheidet.
        key = 0
    return "_" not in str(info.get("id", "")) and key < _MODE_VARIANT_KEY


def sr_champion_data(version: str, cache_dir: Path) -> dict:
    """Champion-Statik OHNE Modus-Varianten (siehe Kommentar oben): dieselbe
    Struktur wie `champions(...)["data"]`, aber nur echte SR-Champions.

    Einzige Quelle fuer alle Lookups, die Varianten nicht sehen duerfen
    (Namensaufloesung, Priors, Klassen-Buckets, Identitaets-Katalog)."""
    data = champions(version, cache_dir)["data"]
    return {cid: info for cid, info in data.items() if _is_sr_champion(info)}


def champion_ids(version: str, cache_dir: Path, names: tuple) -> dict[int, str]:
    """Champion-Namen -> numerische IDs (fuer die Mastery-API)."""
    data = sr_champion_data(version, cache_dir)
    return {int(info["key"]): info["id"] for info in data.values()
            if info["id"] in names or info["name"] in names}


def _simplify(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


@lru_cache(maxsize=4)
def _name_lookup(version: str, cache_dir: Path) -> dict:
    """Vereinfachter Name (id ODER Anzeigename) -> Data-Dragon-ID. Ohne
    Modus-Varianten - sonst ueberschreibt 'Jade_Ashe' den Namenseintrag 'ashe'."""
    data = sr_champion_data(version, cache_dir)
    lookup = {}
    for info in data.values():
        lookup[_simplify(info["id"])] = info["id"]
        lookup[_simplify(info["name"])] = info["id"]
    return lookup


def resolve_name(version: str, cache_dir: Path, name: str) -> str | None:
    """Beliebige Schreibweise/Anzeigename ('bel'veth', 'Bel'Veth', 'wukong')
    -> Data-Dragon-ID ('Belveth', 'MonkeyKing') oder None, wenn unbekannt.
    Die ID ist exakt der championName aus Match-V5."""
    return _name_lookup(version, cache_dir).get(_simplify(name))


def canonical_names(version: str, cache_dir: Path, names: tuple) -> tuple:
    """Wie resolve_name, aber fuer die Pipeline: unbekannte Namen brechen ab."""
    canonical, unknown = [], []
    for name in names:
        match = resolve_name(version, cache_dir, name)
        (canonical if match else unknown).append(match or name)
    if unknown:
        raise SystemExit(f"Unbekannte Champions: {', '.join(unknown)} "
                         f"(Data Dragon {version}).")
    return tuple(dict.fromkeys(canonical))
