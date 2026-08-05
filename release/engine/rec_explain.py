"""Erklaertexte & Item-Tags: leitet aus Tags und benannten Effekten ab, wofuer/
wogegen ein Item gut ist (explain_item) bzw. ein kurzes Buzzword (_item_tag).

Die Badges tragen ZWEI Achsen (Anzeige-only, kein Scoring):
- WOGEGEN das Item hilft: `_item_tag` (Anti-Tank, Anti-AD, Crit-DPS, ...)
- WAS es dir gibt: `_stat_badges` (+HP/+Ruestung/+MR) und `_tag_axis`
  (off/def/hybrid, steuert die Badge-Farbe)
`tag_fields` buendelt beide fuer die Empfehlungs-Payload.

Aus recommend.py ausgelagert (Struktur-Review 2026-07-17, Befund S2). Die zuvor
doppelte Effekt-Regex nutzt jetzt items.effect_names (Befund D4).
"""

from . import items


def _bucket_label(bucket: str | None) -> str:
    """Klassen-Bucket-ID -> lesbares Label: 'ad_fighter' -> 'AD-Fighter',
    'tank' -> 'Tank'."""
    if not bucket:
        return ""
    parts = bucket.split("_")
    if len(parts) == 2:
        return f"{parts[0].upper()}-{parts[1].capitalize()}"
    return bucket.capitalize()


# Bekannte benannte Passiven/Aktiven -> wofuer das Item gut ist.
# Wird gegen die <passive>/<active>-Namen aus den Data-Dragon-Beschreibungen gematcht.
EFFECT_HINTS = {
    "Grievous Wounds": "reduziert gegnerische Heilung",
    "Affliction": "reduziert gegnerische Heilung",
    "Lifeline": "Notfall-Schild gegen Burst",
    "Annul": "Zauberschild blockt die naechste gegnerische Faehigkeit (gegen Pick/Engage)",
    "Immolate": "konstanter Flaechenschaden fuer lange Fights",
    "Cleave": "AoE-Schaden und Waveclear",
    "Spellblade": "verstaerkte Autos nach Faehigkeiten",
    "Awe": "skaliert mit Mana",
    "Stasis": "Aktiv: kurz unverwundbar - gegen Burst und Assassinen",
    "Time Stop": "Aktiv: kurz unverwundbar - gegen Burst und Assassinen",
    "Humility": "Aktiv: verlangsamt Angreifer - gegen Auto-Attack-DPS",
    "Resilience": "reduziert kritischen Schaden",
    "Boundless Vitality": "verstaerkt eigene Heilung und Schilde",
    "Cinderbloom": "Bonus-Schaden auf niedrige Ziele",
}

# Support-Framing (T4b): Text zur Cleanse-Aktive (Mikael's "Purify",
# items.CLEANSE_EFFECTS). BEWUSST NICHT in EFFECT_HINTS - der generische
# explain_item-Pfad iteriert EFFECT_HINTS, ein Eintrag dort wuerde die
# role=None-Ausgabe fuer Mikael's veraendern und die No-Op-Garantie brechen.
# Datengetrieben bleibt die ERKENNUNG (ueber den benannten Effekt), nur der Text
# ist support-lokal.
_CLEANSE_TEXT = "Aktiv: entfernt CC von deinem Carry und heilt ihn"


def _support_tag(name: str, effects: set[str]) -> str | None:
    """Support-Framing der Item-Tags (T4b, nur role == "UTILITY"): ordnet
    Ally-Buff-/Cleanse-/Heal-Shield-Items ihre Support-Rolle zu, BEVOR die
    generische AD/AP/Defense-Kette greift (dort wuerde Mikael's als "Tanky/HP",
    Ardent als "AP-Power" gelabelt - fuer einen Support irrefuehrend).
    Reihenfolge = Spezifitaet. None -> keine Support-Kategorie, generisch weiter."""
    if name in items.ALLY_ONHIT_ITEMS | items.ALLY_AP_ITEMS:
        return "Ally-Buff"
    if effects & items.CLEANSE_EFFECTS:
        return "Peel/Cleanse"
    if name in items.ENCHANTER_ITEMS or items.has_heal_shield_power(name):
        return "Heal/Shield"
    return None


def _support_explain(name: str, effects: set[str]) -> str:
    """Support-Framing der Erklaertexte (T4b, nur role == "UTILITY"): passender
    Begruendungstext zur Support-Kategorie aus _support_tag. Leerer String ->
    keine Support-Kategorie, generischer Text weiter."""
    if name in items.ALLY_ONHIT_ITEMS:
        return "verstaerkt deinen Carry (Attack Speed/On-Hit)"
    if name in items.ALLY_AP_ITEMS:
        return "verstaerkt deinen Carry (AP/Ability-Haste)"
    if effects & items.CLEANSE_EFFECTS:
        # Datengetrieben aus dem benannten Effekt (items.CLEANSE_EFFECTS).
        return _CLEANSE_TEXT
    if name in items.ENCHANTER_ITEMS or items.has_heal_shield_power(name):
        return "Heilung/Schilde fuer dein Team"
    return ""


def explain_item(name: str, split: dict | None = None, top: dict | None = None,
                 role: str | None = None) -> str:
    """Leitet aus Tags und benannten Effekten ab, wogegen/wofuer ein Item gut ist.

    split = AD/AP-Verteilung des Gegnerteams, top = Top-Threat-Profil -
    beides macht die Erklaerung konkret ('gegen die 69% AD im Gegnerteam').

    role (T4b): bei "UTILITY" greifen VOR der generischen Kette die Support-
    Kategorien (Ally-Buff/Cleanse/Heal-Shield). Default None = Verhalten exakt
    wie bisher fuer alle anderen Rollen und Aufrufer ohne Rollen-Kontext.
    """
    entry = items.by_name().get(name)
    if not entry:
        return ""
    item = entry[1]
    tags = set(item.get("tags", []))
    if role == "UTILITY":
        sup = _support_explain(name, items.effect_names(name))
        if sup:
            return sup
    parts: list[str] = []

    # 1. Benannte Passiven/Aktiven mit bekannter Bedeutung
    for effect in items.effect_names(name):
        hint = EFFECT_HINTS.get(effect)
        if hint and hint not in parts:
            parts.append(hint)

    # 2. Defensive Tags, mit Bezug zum Gegnerteam
    if "Armor" in tags:
        if split and split.get("ad", 0) >= 0.55:
            parts.append(f"Ruestung gegen die {split['ad']:.0%} AD im Gegnerteam")
        else:
            parts.append("Ruestung gegen AD-Schaden")
    if "SpellBlock" in tags:
        if split and split.get("ap", 0) >= 0.45:
            parts.append(f"MR gegen die {split['ap']:.0%} AP im Gegnerteam")
        else:
            parts.append("MR gegen AP-Schaden")
    if "Health" in tags and not tags & {"Armor", "SpellBlock"}:
        parts.append("HP gegen gemischten Schaden")

    # 3. Durchdringung (gut gegen Resistenz-Kaeufer/Tanks)
    # Burn-/%-max-HP-Items: DoT auf max-HP - schmilzt Tanks/HP-Stacker (nicht die
    # Pen-Erklaerung, obwohl Liandry's u.a. keinen Pen-Tag tragen).
    if items.is_pct_hp_burn(name) and tags & (items.AD_TAGS | items.AP_TAGS):
        parts.append("Burn-Schaden auf %-max-HP - schmilzt Tanks und HP-Stacker")
    if "MagicPenetration" in tags:
        target = f", z.B. {top['name']}" if top and top.get("build_profile") == "tank" else ""
        parts.append(f"Magiedurchdringung gegen MR-Kaeufe/Tanks{target}")
    # Lethality ist Burst gegen Squishies - die Pen-Erklaerung waere hier falsch.
    if items.is_lethality(name):
        parts.append("Lethality-Burst gegen Squishies - gegen Tanks wenig effektiv")
    elif "ArmorPenetration" in tags:
        parts.append("Ruestungsdurchdringung gegen Tanks/Ruestung")

    # 4. Fallback: reiner Schadens-Spike
    if not parts:
        if "CriticalStrike" in tags:
            parts.append("Crit-Schadens-Spike")
        elif "SpellDamage" in tags:
            parts.append("AP-Spike fuer mehr Schaden")
        elif "Damage" in tags:
            parts.append("AD-Spike fuer mehr Schaden")
        elif "AttackSpeed" in tags:
            parts.append("Angriffstempo/DPS")

    return "; ".join(parts[:2])


def _item_tag(name: str, role: str | None = None) -> str:
    """Kurzes, hervorhebbares Buzzword fuer die Rolle eines Items (Anti-Heal,
    Anti-Tank, Anti-AD, Tanky/HP, Crit-DPS, ...) - abgeleitet aus benannten
    Effekten und Tags. Reihenfolge = Spezifitaet (spezifischstes zuerst).

    role (T4b): bei "UTILITY" greifen VOR der generischen Kette die Support-
    Kategorien (Ally-Buff/Peel-Cleanse/Heal-Shield). Default None = Verhalten
    exakt wie bisher fuer alle anderen Rollen und Aufrufer ohne Rollen-Kontext."""
    entry = items.by_name().get(name)
    if not entry:
        return ""
    item = entry[1]
    tags = set(item.get("tags", []))
    effects = items.effect_names(name)
    if role == "UTILITY":
        sup = _support_tag(name, effects)
        if sup:
            return sup
    if "Boots" in tags:
        return "Mobilität"
    if items.applies_grievous(name):
        return "Anti-Heal"
    if effects & {"Lifeline", "Stasis", "Time Stop", "Annul"}:
        return "Anti-Burst"
    # Flache Lethality ist Burst gegen Squishies - das GEGENTEIL von Anti-Tank
    # (echtes Anti-Tank sind nur %-Pen-Items). VOR der Pen-Tag-Regel, damit
    # Lethality-Items nicht faelschlich als "Anti-Tank" landen. Annul-Effekte
    # (Edge of Night) bleiben davor "Anti-Burst" (Effekt-Check steht frueher).
    if items.is_lethality(name):
        return "AD-Burst/Lethality"
    if tags & {"ArmorPenetration", "MagicPenetration"}:
        return "Anti-Tank"
    if "Armor" in tags:
        return "Anti-AD"
    if "SpellBlock" in tags:
        return "Anti-AP"
    # Burn-/%-max-HP-Items (Liandry's, Demonic Embrace, ...) - stark gegen Tanks/
    # HP-Stacker, keine Burst-AP-Spikes. NACH Armor/SpellBlock (Sunfire -> Anti-AD,
    # Hollow Radiance -> Anti-AP bleiben), VOR dem Offensiv-Block. Die Offensiv-Tag-
    # Bedingung schliesst Tank-Burn-Items (Heartsteel) strukturell aus.
    if items.is_pct_hp_burn(name) and tags & (items.AD_TAGS | items.AP_TAGS):
        return "Burn/%HP"
    # Offensiv-Tags VOR dem Health-Fallback (Review Befund H.2): ein primaeres
    # Schadens-Item mit Health-Nebentag (Gwens "Dusk and Dawn", Trinity Force)
    # ist kein Tank-Item. "Tanky/HP" nur noch, wenn KEINE AD-/AP-Tags vorliegen
    # (gleiche Tag-Mengen wie _is_defensive/_archetype_tilt).
    offensive = tags & (items.AD_TAGS | items.AP_TAGS)
    if offensive:
        if "CriticalStrike" in tags:
            return "Crit-DPS"
        if tags & {"AttackSpeed", "OnHit"}:
            return "On-Hit/DPS"
        if "SpellDamage" in tags:
            return "AP-Power"
        # Rest-Offensiv (z. B. reines LifeSteal ohne "Damage"-Tag): AD-seitig.
        return "AD-Power"
    if "Health" in tags:
        return "Tanky/HP"
    if "CriticalStrike" in tags:
        return "Crit-DPS"
    if tags & {"AttackSpeed", "OnHit"}:
        return "On-Hit/DPS"
    if "SpellDamage" in tags:
        return "AP-Power"
    if "Damage" in tags:
        return "AD-Power"
    return ""


# Natur-Achse: Defensiv-Tag -> Kurz-Badge. Feste Reihenfolge (dict-Order), damit
# die Badge-Folge fuer jedes Item stabil ist: +HP, +Ruestung, +MR.
_STAT_BADGES = {
    "Health": "+HP",
    "Armor": "+Rüstung",
    "SpellBlock": "+MR",
}


def _stat_badges(name: str) -> list[str]:
    """ANZEIGE-ONLY: was das Item dir GIBT (defensive Stats), als kurze Badges.

    Zweite Achse neben `_item_tag` (das sagt, WOGEGEN das Item gut ist). Ein
    Hybrid wie Bloodletter's Curse (AP + HP + MR-Shred) traegt in der Tag-Kette
    nur "Anti-Tank" - die defensive Haelfte wuerde ohne diese Badges unsichtbar
    bleiben.

    Keine Unterdrueckungs-Sonderlogik: auch "Tanky/HP" + "+HP" ist gewollt, und
    Boots mit Armor-Tag (Plated Steelcaps) zeigen "+Ruestung". Unbekanntes Item
    -> leere Liste. Kein Einfluss auf Scoring."""
    tags = items.tags_of(name)
    return [label for tag, label in _STAT_BADGES.items() if tag in tags]


def _tag_axis(name: str) -> str:
    """ANZEIGE-ONLY: Einordnung des Items in offensiv/defensiv/hybrid - Grundlage
    fuer die BADGE-FARBE im Frontend (der Tag-Text allein verriet sie nicht:
    "Anti-Tank" ist offensiv, "Anti-AD" defensiv).

    Gleiche Tag-Mengen wie `_item_tag`: "off" (nur AD-/AP-Tags), "def" (nur
    Defensiv-Tags), "hybrid" (beides), "" (weder/unbekannt). Kein Einfluss auf
    Scoring - insbesondere NICHT mit `_is_defensive` verwandt."""
    tags = items.tags_of(name)
    off = bool(tags & (items.AD_TAGS | items.AP_TAGS))
    deff = bool(tags & items.DEF_TAGS)
    if off and deff:
        return "hybrid"
    if off:
        return "off"
    if deff:
        return "def"
    return ""


def tag_fields(name: str, role: str | None = None) -> dict:
    """Alle Anzeige-Tag-Felder eines Items in einem Rutsch - der EINE Ort, an dem
    Empfehlungs-Dicts ihre Badge-Daten holen (`**tag_fields(...)`).

    Vertrag: {"tag": <Zweck-Buzzword>, "tag_axis": <off|def|hybrid|"">,
    "stats": [<+HP|+Ruestung|+MR>]}."""
    return {"tag": _item_tag(name, role=role),
            "tag_axis": _tag_axis(name),
            "stats": _stat_badges(name)}


def _is_defensive(item_name: str, vs: str) -> bool:
    """vs: 'ad' oder 'ap' - passt das Item gegen diesen Schadenstyp?"""
    tags = items.tags_of(item_name)
    if vs == "ad":
        return bool(tags & {"Armor"}) or "Health" in tags
    return bool(tags & {"SpellBlock"}) or "Health" in tags
