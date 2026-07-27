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


def champion_ids(version: str, cache_dir: Path, names: tuple) -> dict[int, str]:
    """Champion-Namen -> numerische IDs (fuer die Mastery-API)."""
    data = champions(version, cache_dir)["data"]
    return {int(info["key"]): info["id"] for info in data.values()
            if info["id"] in names or info["name"] in names}


def _simplify(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


@lru_cache(maxsize=4)
def _name_lookup(version: str, cache_dir: Path) -> dict:
    """Vereinfachter Name (id ODER Anzeigename) -> Data-Dragon-ID."""
    data = champions(version, cache_dir)["data"]
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
