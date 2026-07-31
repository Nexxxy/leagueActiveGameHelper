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


def source_path() -> Path | None:
    """Pfad der builds.yaml, die `load()` tatsaechlich liest (die gepinnte Quelle
    oder die neueste generierte), None wenn keine existiert.

    Oeffentlich, damit die Diagnose (engine.manifest) den Pfad nicht nachbaut -
    eine Quelle der Wahrheit statt zweier, die auseinanderlaufen koennen."""
    if _PINNED is not None:
        return _PINNED
    generated = ROOT / "knowledge" / "generated"
    candidates = sorted(
        generated.glob("*/builds.yaml"),
        key=lambda p: [int(x) for x in p.parent.name.split(".") if x.isdigit()],
    )
    return candidates[-1] if candidates else None


def overrides_path() -> Path:
    """Pfad der kuratierten Overrides (existiert nicht zwingend)."""
    return ROOT / "knowledge" / "curated" / "overrides.yaml"


def _latest_builds() -> tuple[str, dict]:
    path = source_path()
    if path is None:
        return "", {"champions": {}}
    # Patch-Name aus dem Elternverzeichnis; leer -> "backtest" (gepinnte Quelle
    # ohne Patch-Ordner, nur Backtest/Tests).
    patch = path.parent.name or "backtest"
    return patch, yaml.safe_load(path.read_text(encoding="utf-8"))


def _overrides() -> dict:
    path = overrides_path()
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
    cache_dir = Config.load().cache_dir
    # Offline-tolerant: ohne Netz die neueste vollstaendig gecachte Version.
    return ddragon.latest_version_cached(cache_dir), cache_dir


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


def next_after(champion: str, role: str | None = None) -> dict:
    """Uebergangs-Bigramm des Champion+Rollen-Eintrags (pipeline T1):
    `{<Vorgaenger-Item>: [{item, count, win_rate}, ...]}` - was Spieler nach
    einem bestimmten FERTIGEN Item als naechstes fertiges Item gebaut haben.

    Leeres Dict, wenn die Kombi unbekannt ist ODER die builds.yaml den Block
    nicht kennt (alte/gepinnte KBs, Test-Fixtures). Die Leseseite faellt dann
    auf einen neutralen Lift zurueck - der Block ist rein additiv."""
    _role, entry = for_champion(champion, role)
    return entry.get("next_after") or {}


def slot_dist(champion: str, role: str | None = None) -> dict[str, dict[int, float]]:
    """Item-Name -> {Kaufslot: Anteil} (Pipeline V2-04, `slot_dist`) fuer alle
    Items des Champion+Rollen-Eintrags (core UND situational).

    Leeres Dict fuer KBs vor V2-04; Items ohne belastbare Slot-Verteilung fehlen
    einzeln. Slot-Keys werden defensiv nach int gezogen (ein handgeschriebener
    Override koennte sie als String liefern), unbrauchbare Eintraege fallen
    still weg - die Leseseite soll an einer kaputten Zelle nicht sterben."""
    _role, entry = for_champion(champion, role)
    out: dict[str, dict[int, float]] = {}
    for item in list(entry.get("core") or []) + list(entry.get("situational") or []):
        dist = item.get("slot_dist") if isinstance(item, dict) else None
        if not isinstance(dist, dict):
            continue
        clean = {}
        for slot, share in dist.items():
            try:
                clean[int(slot)] = float(share)
            except (TypeError, ValueError):
                continue
        if clean:
            out[item["item"]] = clean
    return out


def exclusive_pairs(champion: str, role: str | None = None) -> list[frozenset]:
    """Gelernte Exklusiv-Paare des Champion+Rollen-Eintrags (Pipeline V2-04) als
    Liste von frozensets mit zwei Item-Namen - Ergaenzung zu `items.conflicts`.

    Leere Liste fuer KBs vor V2-04. Eintraege, die kein Zweier-Paar sind, werden
    ausgelassen (statt eine Exception zu werfen)."""
    _role, entry = for_champion(champion, role)
    out: list[frozenset] = []
    for pair in entry.get("exclusive") or []:
        names = [str(x) for x in pair] if isinstance(pair, (list, tuple)) else []
        if len(names) == 2:
            out.append(frozenset(names))
    return out


def boots_cells(champion: str, role: str | None = None) -> dict:
    """Konditionale Boots-Zellen des Champion+Rollen-Eintrags (Pipeline V2-02):

        {"by_threat": {ad|ap: {games, base_win_rate, items}},
         "by_cc":     {cc_heavy|cc_light: {...}},
         "by_state":  {ahead|behind: {purchases, base_win_rate, items}}}

    Jeder Block fehlt einzeln, wenn die Datenlage ihn nicht hergibt - und ALLE
    fehlen in KBs, die vor V2-02 gebaut wurden. Die Leseseite bekommt dann leere
    Dicts und faellt auf die unkonditionierte Boots-Liste zurueck."""
    _role, entry = for_champion(champion, role)
    return {"by_threat": entry.get("boots_by_threat") or {},
            "by_cc": entry.get("boots_by_cc") or {},
            "by_state": entry.get("boots_by_state") or {}}


def for_class(bucket: str | None, role: str | None) -> dict:
    """Liefert den Klassen-Eintrag (Bucket + Rolle) aus der `classes:`-Sektion
    der builds.yaml - Fallback fuer duenne Champion-Kombis. Leeres Dict, wenn
    Bucket/Rolle unbekannt sind (kein sinnvoller rollenfremder Fallback)."""
    if not bucket or not role:
        return {}
    return load().get("classes", {}).get(bucket, {}).get(role, {})


def class_by_state(bucket: str | None, role: str | None) -> dict:
    """Gold-konditionierter Klassen-Block (Pipeline V2-07):
    `{ahead|behind: {purchases, base_win_rate, items}}`.

    Zweite Quelle der Behind-Auswahl, wenn die Champion-eigene Zelle zu duenn
    ist. Leeres Dict fuer unbekannte Buckets UND fuer KBs vor V2-07 - der
    Aufrufer faellt dann auf die naechste Stufe (defensiv getaggte Items)."""
    return for_class(bucket, role).get("by_state") or {}
