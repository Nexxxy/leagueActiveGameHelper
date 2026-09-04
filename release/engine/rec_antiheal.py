"""Anti-Heal-Schicht: erkennt gefedete High-Sustain-Gegner und schlaegt ein
Grievous-Wounds-Item als Option vor, wenn niemand im Team es traegt.

Aus recommend.py ausgelagert (Struktur-Review 2026-07-17, Befund S2).
"""

from core.ddragon import _simplify as _canon_champ

from . import items, profiling
from .rec_explain import _is_defensive_item, tag_fields


# Guenstige Grievous-Komponenten (800 G) je Schadenstyp: wenn man hinten liegt,
# holt man den Anti-Heal-Effekt billig, statt Gold in ein teures Offensiv-Item
# (Mortal Reminder & Co., ~3000 G) zu stecken.
_ANTIHEAL_CHEAP = {"ad": "Executioner's Calling", "ap": "Oblivion Orb"}


# High-Sustain-Champions als stabile Data-Dragon-IDs (Fix 5.7 - locale-fest,
# nicht mehr am Live-Anzeigenamen). Gegen die lohnt Anti-Heal (Grievous Wounds)
# besonders. Kuratiert - Faehigkeiten-Heilung steht nicht zuverlaessig in den
# Item-/Champion-Daten. Fallback, bis datengetriebene konditionale Stats genug
# Samples haben.
#
# Kuratierung (Fix 5.1):
#   - 'Ryze' ENTFERNT: hat keine nennenswerte Selbstheilung, war ein Fehleintrag.
#   - 'Sett' ENTFERNT: nur Graues-Leben-Sustain (grenzwertig), kein echter
#     Heal-Champ - rechtfertigt kein Grievous-Item.
#   - 'Kayn' BLEIBT: heilt nur als Rhaast, aber die Live-API unterscheidet die
#     Form nicht zuverlaessig. Der Fed-Trigger unten daempft Blue-Kayn-Fehlalarme
#     (ein nicht-gefedeter Kayn loest keine Empfehlung mehr aus).
#
# OEFFENTLICH (KB-Dimension by_heal): die Liste ist seit der by_heal-Dimension
# die EINE Quelle fuer Train (pipeline/aggregate.py zaehlt die Heal-Spiele) UND
# Serve (engine/rec_context._enemy_heal_bucket). Darum ohne Unterstrich - eine
# zweite, kopierte Liste in der Pipeline waere genau der Train/Serve-Skew, den
# Review-Befund G verboten hat.
HEALING_THREATS = {
    "Aatrox", "Swain", "Warwick", "Vladimir", "DrMundo", "Sylas", "Zac",
    "Briar", "Fiddlesticks", "Soraka", "Yuumi", "Nami", "Renata",
    "Ivern", "Kayn", "Illaoi", "Nasus", "Aurora",
}

# Ab so vielen Heilern im Gegnerteam gilt es als "heal-lastig" (KB-Bucket
# `heal_heavy`). Nutzer-Entscheid 2026-08-08: EIN Bucket, kein Komplement - ein
# "nicht heal-lastig"-Bucket waere nur das Marginal-Aggregat.
HEAL_HEAVY_MIN = 2

# Vereinfachte (lowercase, nur alnum) Schreibweisen der Liste - Vergleichsform
# fuer `heal_bucket`. WARUM nicht der rohe Set-Check: die beiden Quellen, die den
# Bucket bilden, schreiben denselben Champion NICHT gleich.
#   - Serve: `champion_id` aus profiling ist bereits die kanonische DD-ID
#     ("Fiddlesticks"), weil `verified_champion_id` gegen den DD-Katalog aufloest.
#   - Train: der Matchindex legt Riots `championName` VERBATIM ab - und der ist
#     bei Fiddlesticks "FiddleSticks" (grosses S). Ein roher Set-Check haette den
#     Champion auf der Trainseite stumm verloren (1 von 18 Heilern), waehrend die
#     Serveseite ihn zaehlt - genau der Train/Serve-Skew, den Review-Befund G
#     verbietet. Dieselbe Casing-Falle wie Befund C (2026-07-13).
# `_simplify` ist bewusst DIE Funktion, mit der `core.ddragon` seinen
# Namens-Lookup baut (gleiche Praxis wie profiling.verified_champion_id) - so
# gibt es keine zweite, leicht abweichende Kanonisierungsregel.
_HEALING_CANON = frozenset(_canon_champ(c) for c in HEALING_THREATS)


def heal_bucket(champion_ids) -> str | None:
    """'heal_heavy' | None - Heal-Last eines Teams aus den Champion-Namen/-IDs.

    DIE gemeinsame Bucket-Definition fuer Train und Serve (Muster
    `champions.damage_bucket` fuer by_threat, Review-Befund G): die
    pipeline-Aggregation zaehlt ihre `by_heal`-Zellen damit, und die Engine
    fragt sie damit ab. Nur ein Bucket - unter HEAL_HEAVY_MIN Heilern gibt es
    kein Signal, nicht das Gegenteil.

    Akzeptiert jede Schreibweise der DD-ID (s. `_HEALING_CANON`); Anzeigenamen
    mit anderem Wortstamm ('Wukong' fuer 'MonkeyKing') werden NICHT aufgeloest -
    beide Aufrufer liefern DD-IDs."""
    n = sum(1 for cid in champion_ids
            if cid and _canon_champ(cid) in _HEALING_CANON)
    return "heal_heavy" if n >= HEAL_HEAVY_MIN else None


def _has_grievous(item_names) -> bool:
    # Erkennung ueber den Beschreibungstext (items.applies_grievous), nicht mehr
    # ueber den Passiv-Namen: faengt umbenannte Passiven wie Chempunk Chainsword
    # ('Hackshorn') und Chemtech Putrifier ('Puffcap Toxin') mit ab.
    return any(items.applies_grievous(n) for n in item_names)


def _antiheal_items(vs: str) -> list[str]:
    """Fertige Items mit Grievous Wounds, passend zum eigenen Schadenstyp."""
    out = []
    for name in items.grievous_names():
        entry = items.by_name().get(name)
        if not entry or entry[1].get("into"):
            continue
        tags = set(entry[1].get("tags", []))
        if vs == "ap" and tags & items.AP_TAGS:
            out.append(name)
        elif vs == "ad" and tags & items.AD_TAGS:
            out.append(name)
    return out


def _my_damage_type(owned_ids: list[int], core_source: list[dict]) -> str:
    """'ad' | 'ap' - aus dem eigenen Inventar, frueh aus den Core-Items."""
    buckets = items.categorize_gold(owned_ids)["buckets"] if owned_ids else {}
    if buckets.get("ad") or buckets.get("ap"):
        return "ap" if buckets["ap"] >= buckets["ad"] else "ad"
    ad = ap = 0
    for it in core_source:
        tags = items.tags_of(it["item"])
        ad += len(tags & items.AD_TAGS)
        ap += len(tags & items.AP_TAGS)
    return "ap" if ap > ad else "ad"


def _fed_healers(enemy_profiles: list[dict], game_time: float) -> list[dict]:
    """High-Sustain-Gegner, die ABSOLUT gefedet genug fuer Anti-Heal sind
    (Review-Befund F): profiling.is_fed_enough (>= 1 fertiges Item + Gold ueber
    der zeitabhaengigen Erwartung) statt der frueheren Team-Median-Bedingung
    (die per Definition fast immer wahr war). Ein normal farmender 0/0/0-Warwick
    triggert damit nicht mehr - im Zweifel gar nicht triggern."""
    return [e for e in enemy_profiles
            if e.get("champion_id") in HEALING_THREATS
            and profiling.is_fed_enough(e, game_time)]


def _antiheal_recommendation(enemy_profiles: list[dict], owned_names: set[str],
                             ally_items: set[str], situational_source: list[dict],
                             my_dmg: str, game_time: float,
                             struggling: bool = False) -> dict | None:
    """Schicht 4 (kuratiert): Hat das Gegnerteam einen GEFEDETEN High-Sustain-
    Champion (siehe _fed_healers) und traegt WEDER der Spieler NOCH ein
    Mitspieler bereits Anti-Heal, wird ein Grievous-Item als OPTION vorgeschlagen.
    Wer hinten liegt/defensiv spielt (`struggling`), bekommt die GUENSTIGE
    Komponente (800 G) statt eines teuren Offensiv-Items. Rueckgabe: rec-Dict
    oder None."""
    fed = _fed_healers(enemy_profiles, game_time)
    if not fed:
        return None
    if _has_grievous(owned_names) or _has_grievous(ally_items):
        return None  # Team ist abgedeckt
    heal_list = ", ".join(e["name"] for e in fed)
    if struggling:
        cheap = _ANTIHEAL_CHEAP.get(my_dmg)
        if cheap and cheap not in owned_names and cheap in items.by_name():
            # `tag_fields` liefert die beiden Anzeige-Achsen (Farbe + Stat-
            # Badges); der Zweck-Tag bleibt der kuratierte "Anti-Heal".
            # `defensive` wird aus dem konkreten Item ABGELEITET statt hart auf
            # True gesetzt: Anti-Heal ist eine eigene Achse, kein Ersatz fuer
            # Ruestung/MR (Executioner's Calling und Oblivion Orb sind reine
            # Schadens-Komponenten, Bramble Vest waere echt defensiv).
            return {"item": cheap, "kind": "situational",
                    "defensive": _is_defensive_item(cheap),
                    "antiheal": True, **tag_fields(cheap), "tag": "Anti-Heal",
                    "reason": (f"Anti-Heal gegen {heal_list} - aber du liegst "
                               f"hinten: nur die guenstige Komponente ({cheap}, "
                               f"800 G), kein teures Item forcieren.")}
    candidates = _antiheal_items(my_dmg)
    if not candidates:
        return None
    # Bevorzugt ein Grievous-Item, das im Build der Rolle ohnehin vorkommt.
    in_build = [s["item"] for s in situational_source if s["item"] in candidates]
    pick = next((c for c in in_build if c not in owned_names),
                next((c for c in candidates if c not in owned_names), None))
    if pick is None:
        return None
    # `defensive` abgeleitet, nicht hart True (s. cheap-Zweig oben): die
    # Vollitems dieser Liste (Mortal Reminder, Morellonomicon, ...) sind
    # Schadens-Items mit Grievous-Passive und keine Defensiv-Kaeufe.
    return {"item": pick, "kind": "situational",
            "defensive": _is_defensive_item(pick),
            "antiheal": True, **tag_fields(pick), "tag": "Anti-Heal",
            "reason": (f"Anti-Heal gegen {heal_list} - niemand im Team hat "
                       f"Grievous Wounds. Reduziert ihre Heilung um 40% "
                       f"(Option, kein Pflichtkauf).")}
