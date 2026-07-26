"""Laedt die Wissensbasis: neueste generierte builds.yaml + kuratierte Overrides."""

from functools import lru_cache
from pathlib import Path

import yaml

from core.config import ROOT


# Optional gepinnte Quelle (nur fuer Backtest/Tests): zeigt auf eine konkrete
# builds.yaml statt auf die neueste generierte. None = Default-Verhalten.
_PINNED: Path | None = None


def set_source(path: Path | None) -> None:
    """Pinnt eine konkrete builds.yaml als Quelle (None = zurueck zum Default,
    also die neueste knowledge/generated/*/builds.yaml). Leert die betroffenen
    Caches, damit die naechste load()-Auswertung die neue Quelle sieht.

    NUR fuer Backtest/Tests gedacht - NICHT im Live-Server verwenden."""
    global _PINNED
    _PINNED = Path(path) if path is not None else None
    load.cache_clear()
    _simplified_keys.cache_clear()


def _latest_builds() -> tuple[str, dict]:
    if _PINNED is not None:
        # Patch-Name aus dem Elternverzeichnis der gepinnten Datei; leer -> "backtest".
        patch = _PINNED.parent.name or "backtest"
        return patch, yaml.safe_load(_PINNED.read_text(encoding="utf-8"))
    generated = ROOT / "knowledge" / "generated"
    candidates = sorted(
        generated.glob("*/builds.yaml"),
        key=lambda p: [int(x) for x in p.parent.name.split(".") if x.isdigit()],
    )
    if not candidates:
        return "", {"champions": {}}
    path = candidates[-1]
    return path.parent.name, yaml.safe_load(path.read_text(encoding="utf-8"))


def _overrides() -> dict:
    path = ROOT / "knowledge" / "curated" / "overrides.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def load() -> dict:
    patch, data = _latest_builds()
    champions = data.get("champions", {})
    # Overrides mergen nur Einzelfelder in den Eintrag - `source_patch` (Provenienz
    # aus pipeline/inherit.py) bleibt dabei erhalten, solange ein Override ihn
    # nicht selbst setzt. Genau so gewollt: ein kuratierter Override aendert den
    # Ursprung der Zahlen nicht.
    for champ, roles in _overrides().get("champions", {}).items():
        for role, override in roles.items():
            champions.setdefault(champ, {}).setdefault(role, {}).update(override)
    return {"patch": patch, "champions": champions,
            # Patch, aus dem geerbte Eintraege stammen (None = nichts geerbt).
            "inherited_from": data.get("inherited_from"),
            "classes": data.get("classes", {}),
            "cc_priors": data.get("cc_priors", {}),
            # Globales Komponenten-Reihenfolge-Aggregat (Feature 001, Fallback-
            # Stufe 2 der Kaufplan-Leiste): {<target_iid als str>: {n, order}}.
            "component_order_global": data.get("component_order_global", {})}


def component_order_global() -> dict:
    """Globales Komponenten-Reihenfolge-Aggregat aus der builds.yaml (Feature 001,
    Fallback-Stufe 2 der Kaufplan-Leiste, wenn der Champion+Rolle-Eintrag fuer ein
    Ziel-Item keine gelernte `component_order` hat). Format wie je Champion:
    {<target_iid als str>: {"n": <int>, "order": [<comp_iid als int>, ...]}}."""
    return load().get("component_order_global", {})


def cc_prior_for_id(cid: str | None) -> float | None:
    """Data-Dragon-ID -> cc_per_min (CC-Sekunden/Minute) aus dem cc_priors-Block
    der builds.yaml, oder None wenn der Champion keinen Prior hat. Analog zu
    champions.ad_share_for_id: 'unbekannt' bleibt None (der Aufrufer laesst solche
    Gegner beim Team-Mittel weg)."""
    if not cid:
        return None
    entry = load().get("cc_priors", {}).get(cid)
    return entry.get("cc_per_min") if entry else None


@lru_cache(maxsize=1)
def _resolver_ctx() -> tuple:
    """Version + Cache-Pfad fuer die Namensaufloesung, einmal pro Prozess."""
    from core import ddragon
    from core.config import Config
    return ddragon.latest_version(), Config.load().cache_dir


@lru_cache(maxsize=1)
def _simplified_keys() -> dict[str, str]:
    """Vereinfachter KB-Key ('fiddlesticks') -> tatsaechlicher KB-Key
    ('FiddleSticks'). Faengt die Riot-Inkonsistenz ab, dass der Match-V5-
    championName (KB-Key) in der Gross-/Kleinschreibung von der Data-Dragon-ID
    abweichen kann (bekannt: 'FiddleSticks' vs. 'Fiddlesticks')."""
    from core.ddragon import _simplify
    return {_simplify(k): k for k in load()["champions"]}


def _canonical(champion: str) -> str:
    """Champion-Kennung (Live-Anzeigename, Data-Dragon-ID oder Match-championName)
    -> tatsaechlicher KB-Key. Robust gegen Casing-Abweichungen (Fix 5.7): erst
    direkter Treffer, dann vereinfachter Abgleich gegen die KB-Keys, zuletzt die
    Data-Dragon-Namensaufloesung (ebenfalls vereinfacht abgeglichen). Bei Miss
    bleibt der Name unveraendert."""
    champions = load()["champions"]
    if champion in champions:
        return champion
    from core.ddragon import _simplify
    simp = _simplified_keys()
    hit = simp.get(_simplify(champion))
    if hit:
        return hit
    from core import ddragon
    resolved = ddragon.resolve_name(*_resolver_ctx(), champion)
    if resolved:
        return simp.get(_simplify(resolved), resolved)
    return champion


def for_champion(champion: str, role: str | None = None) -> tuple[str, dict]:
    """Liefert (rolle, eintrag). Ohne Rolle wird die meistgespielte genommen."""
    roles = load()["champions"].get(_canonical(champion), {})
    if not roles:
        return "", {}
    if role and role in roles:
        return role, roles[role]
    best = max(roles, key=lambda r: roles[r].get("games", 0))
    return best, roles[best]


def for_class(bucket: str | None, role: str | None) -> dict:
    """Liefert den Klassen-Eintrag (Bucket + Rolle) aus der `classes:`-Sektion
    der builds.yaml - Fallback fuer duenne Champion-Kombis. Leeres Dict, wenn
    Bucket/Rolle unbekannt sind (kein sinnvoller rollenfremder Fallback)."""
    if not bucket or not role:
        return {}
    return load().get("classes", {}).get(bucket, {}).get(role, {})
