"""Item-Lookup auf Basis von Data Dragon (gecacht durch die Pipeline)."""

import re
from collections import Counter
from functools import lru_cache

from core import ddragon
from core.config import Config

# EINE kanonische Tag-Taxonomie (Fix 5.6): AD/AP/DEF. Einzige Quelle fuer die
# Schadenstyp-Einordnung im gesamten app-Code (categorize_gold, recommend.
# _my_damage_type, _antiheal_items, _archetype_tilt). Vorher divergierten drei
# Mengen und Hybrid-Items landeten je nach Codepfad in verschiedenen Buckets.
#
# AttackSpeed und OnHit zaehlen zu AD: On-Hit-/Angriffstempo-Items sind im
# Pool-Kontext physisch dominiert (Kraken, BorK, Trinity). Echte AD/AP-Hybride
# (Nashor's Tooth) tragen zusaetzlich SpellDamage und werden von categorize_gold
# dadurch anteilig auf beide Buckets aufgeteilt - die AP-Ratio faengt sie ab,
# ohne dass es eine Sonderliste braucht.
AD_TAGS = {"Damage", "CriticalStrike", "AttackSpeed", "OnHit",
           "ArmorPenetration", "LifeSteal"}
AP_TAGS = {"SpellDamage", "MagicPenetration"}
DEF_TAGS = {"Armor", "SpellBlock", "Health"}

# Ally-Buff-Items fuer die Botlane-Partner-Achse (research_bot_sup_mates.md 9.40).
# Kuratierte, name-basierte Listen: diese Items verstaerken den Verbuendeten und
# sind ueber die Data-Dragon-Tags NICHT identifizierbar (der Buff-Effekt steckt in
# der Passive, nicht in den Tags). Der Empfehlungs-Layer (T4) matcht sie an den
# Schadenstyp des eigenen Bot-Partners: On-Hit-Buff nur bei AD-Partner, AP-Buff
# nur bei AP-Partner.
ALLY_ONHIT_ITEMS = {"Ardent Censer"}            # bufft Attack Speed / On-Hit des Allys
ALLY_AP_ITEMS = {"Staff of Flowing Water"}      # bufft AP / Ability Haste des Allys
# Klassische Enchanter-Items fuer den weichen Archetyp-Tilt (T4): bei AP-Partner
# dreht der Support seltener Heal/Shield-Enchanter (Befund 2, 9.40).
ENCHANTER_ITEMS = {"Dream Maker", "Moonstone Renewer", "Echoes of Helia",
                   "Mikael's Blessing", "Ardent Censer", "Staff of Flowing Water"}
# Benannte Aktive, die gegnerisches CC vom Verbuendeten entfernen (Cleanse/Peel).
# Datengetrieben aus den Data-Dragon-Effektnamen: Mikael's Blessing traegt die
# benannte Aktive "Purify" (verifiziert gegen item_16.14.1.json). Fuer das
# Support-Framing der Item-Tags (T4b) - ein Support baut Mikael's wegen Cleanse,
# nicht wegen der HP/AbilityHaste-Stats.
CLEANSE_EFFECTS = {"Purify"}

# Fertige Quest-Formen der Support-Item-Linie. Sie tragen zwar die Support-Tags
# (GoldPer+Lane) und gold.total==400 (keine 2000-G-Powerspike-Schwelle), sind aber
# das FERTIGE Support-Item eines Supports - fuer die Live-Spike-Warnung zaehlen sie
# darum als abgeschlossenes Item (sonst unterzaehlt count_completed den Support um 1
# -> falsche Spike-Warnungen + off-by-one im "N. fertiges Item"-Text).
#
# NUR die Endformen des aktuellen World-Atlas-Quest-Baums auf Summoner's Rift
# (map11=True, into=None): World Atlas -> Runic Compass -> Bounty of Worlds -> eine
# der fuenf Wahlformen. Basis-/Mittelstufen (World Atlas, Runic Compass) und die
# Zwischenstufe "Bounty of Worlds" (into gesetzt) gehoeren NICHT hierher - sie sind
# noch kein fertiges Power-Item. Die Legacy-Linien (Shard of True Ice, Black Mist
# Scythe, Pauldrons of Whiterock, Bulwark of the Mountain) sind map11=False (nicht
# auf SR) und tauchen in Live-SR-Inventaren nie auf - bewusst ausgelassen.
COMPLETED_SUPPORT_ITEMS = {"Bloodsong", "Celestial Opposition", "Dream Maker",
                           "Zaz'Zak's Realmspike", "Solstice Sleigh"}

# ID-Pendant zur World-Atlas-Questkette auf Summoner's Rift (Basis -> Mittel ->
# Zwischenform): World Atlas (3865) -> Runic Compass (3866) -> Bounty of Worlds
# (3867). Besitzt der Support eines dieser Items, ist die Quest noch offen und die
# fuenf Endformen (COMPLETED_SUPPORT_ITEMS) stehen als Wahl an. Als ID-Menge, weil
# der Empfehlungs-Layer den Besitz ueber owned_ids prueft (robuster als Namen).
SUPPORT_QUEST_IDS = {3865, 3866, 3867}

# Das defensive Schild-Item unter den fuenf Endformen (Item-ID 3869). Der
# Support-Final-Layer bietet es als defensiven Zweitvorschlag an, wenn die Lage
# defensiv ist und es nicht ohnehin der champion-feste Primaervorschlag ist.
CELESTIAL_OPPOSITION = "Celestial Opposition"


# Summoner's-Rift-Filter (Scope des Projekts: Ranked/Normal Draft, SR 5v5).
#
# WARUM: Data Dragon fuehrt zu fast jedem Item MODUS-VARIANTEN mit eigener,
# 6-stelliger ID (Praefix 22/32/44/66/77 + Basis-ID) und IDENTISCHEM
# Anzeigenamen. In 16.15.1 sind das 439 von 868 Eintraegen. Weil `by_name()`
# ueber den Namen indiziert, hat die zuletzt gelesene Variante den echten
# SR-Eintrag ueberschrieben - mit falscher Beschreibung, falschem Preis und
# falschem Rezept. Live verifiziert: 773100 "Lich Bane" (Jade-Modus, Passive
# als <jadeUnique> statt <passive>) verdraengte 3100; damit fand
# `passive_names()` fuer Lich Bane keine Passive mehr und `conflicts()` liess
# Dusk and Dawn trotz Spellblade-Kollision durch.
#
# Zwei Kriterien, beide noetig:
# 1. maps["11"] - schliesst ARAM-/Arena-/Swarm-/Jade-only-Items aus.
# 2. ID < 100000 - die 6-stelligen Varianten-IDs. 43 von ihnen behaupten
#    trotzdem maps["11"] (z.B. 323075 "Thornmail", 323003 "Archangel's Staff",
#    667666 "The Collector"); ohne dieses zweite Kriterium wuerden sie die
#    echten SR-Items weiterhin verdraengen - inklusive ihrer Varianten-Rezepte,
#    die auf Komponenten-IDs zeigen, die es im Live-Inventar nie gibt.
#    Riots echter Item-Katalog ist durchgaengig 4-stellig (in 16.15.1 gibt es
#    keine einzige 5-stellige ID), die 6-stelligen sind reine Modus-Namensraeume.
_SR_MAP = "11"
_MODE_VARIANT_ID = 100000


def _is_sr_item(item_id: str, item: dict) -> bool:
    return (item_id.isdigit() and int(item_id) < _MODE_VARIANT_ID
            and bool(item.get("maps", {}).get(_SR_MAP)))


@lru_cache(maxsize=1)
def _load_raw() -> tuple[str, dict]:
    """UNGEFILTERTE Data-Dragon-Items (alle Modi). Nur fuer Auswertungen, die
    bewusst ueber alle Varianten eines Namens scannen (grievous_names) - fuer
    alles andere ist `_load()` die richtige Quelle."""
    cfg = Config.load()
    # Offline-tolerant: ohne Netz die neueste vollstaendig gecachte Version.
    version = ddragon.latest_version_cached(cfg.cache_dir)
    return version, ddragon.items(version, cfg.cache_dir)["data"]


@lru_cache(maxsize=1)
def _load() -> tuple[str, dict]:
    version, data = _load_raw()
    return version, {i: it for i, it in data.items() if _is_sr_item(i, it)}


def version() -> str:
    return _load()[0]


def by_id(item_id: int) -> dict | None:
    return _load()[1].get(str(item_id))


def name_of(item_id: int) -> str:
    item = by_id(item_id)
    return item["name"] if item else f"Item {item_id}"


@lru_cache(maxsize=1)
def by_name() -> dict[str, tuple[int, dict]]:
    return {item["name"]: (int(item_id), item)
            for item_id, item in _load()[1].items()}


@lru_cache(maxsize=1)
def passive_names() -> dict[str, frozenset]:
    """Item-Name -> Namen seiner benannten Passiven (z.B. 'Spellblade').

    Items mit derselben benannten Passive schliessen sich im Spiel aus
    bzw. stacken nicht - sie duerfen nicht zusammen empfohlen werden.
    """
    result = {}
    for name, (_, item) in by_name().items():
        found = frozenset(re.findall(r"<passive>([^<]+)</passive>",
                                     item.get("description", "")))
        if found:
            result[name] = found
    return result


@lru_cache(maxsize=1)
def grievous_names() -> frozenset:
    """Item-Namen, deren Beschreibung Grievous Wounds anwendet.

    Robust gegen umbenannte Passiven (Chempunk Chainsword 'Hackshorn',
    Chemtech Putrifier 'Puffcap Toxin' - beide heissen NICHT mehr 'Grievous
    Wounds', wenden es aber laut Beschreibungstext an): Kriterium ist der
    Beschreibungstext, nicht der <passive>-Name.

    Gescannt werden ALLE Item-Eintraege (`_load_raw()`, nicht das SR-gefilterte
    `_load()`/`by_name()`): Data Dragon fuehrt denselben Namen mehrfach (SR-ID
    6609 vs. Varianten-ID 226609), und nur EINE Variante nennt 'Grievous Wounds'
    woertlich (die andere kuerzt zu '40% Wounds'). Nennt IRGENDEINE Variante den
    Effekt woertlich, gilt der Name als Grievous-Item - so faellt Chempunk
    Chainsword nicht durchs Raster.

    Genau darum haengt diese eine Funktion an den ungefilterten Daten: bei
    Chempunk Chainsword (6609) UND Thornmail (3075) steht 'Grievous Wounds'
    woertlich nur in der Nicht-SR-Variante. Ueber `_load()` wuerden beide aus
    der Antiheal-Erkennung fallen."""
    return frozenset(
        item["name"] for item in _load_raw()[1].values()
        if "Grievous Wounds" in item.get("description", "")
    )


def applies_grievous(item_name: str) -> bool:
    """True, wenn das Item Grievous Wounds anwendet (Beschreibungstext), auch
    wenn die benannte Passive anders heisst."""
    return item_name in grievous_names()


_EFFECT_RE = re.compile(
    r"<(?:passive|active|keyword)>([^<]+)</(?:passive|active|keyword)>")


def effect_names(item_name: str) -> set[str]:
    """Benannte Passiven/Aktiven/Keywords eines Items als Menge (gestrippt um
    ' -:'). Geteilte Quelle fuer explain_item und _item_tag (Befund D4,
    Struktur-Review 2026-07-17): parst die <passive>/<active>/<keyword>-Namen
    aus der Data-Dragon-Beschreibung. Leere Menge bei unbekanntem Item."""
    entry = by_name().get(item_name)
    if not entry:
        return set()
    return {e.strip(" -:")
            for e in _EFFECT_RE.findall(entry[1].get("description", ""))}


# Burn-/%-max-HP-Marker (beschreibungsbasiert, patch-stabil wie applies_grievous):
# echte DoT-/Burn-Items nennen "burn" woertlich (Liandry's, Demonic Embrace,
# Malignance) oder tragen ihre Feuer-Passive namentlich im Text (Blackfire Torch
# -> "Baleful Blaze"/"Blackfire"). BEWUSST gegen False-Positives geprueft:
# Warmog's (nur HP-Regen), Heartsteel (max-Health-Skalierung, Tank), Sunfire/
# Hollow Radiance (Immolate, kein "burn"-Wort) matchen NICHT.
_BURN_RE = re.compile(r"\bburn|blaze|blackfire", re.I)


def is_pct_hp_burn(item_name: str) -> bool:
    """True, wenn die Data-Dragon-Beschreibung Burn-/%-max-HP-Schaden anwendet
    (DoT-Items wie Liandry's, Demonic Embrace, Malignance, Blackfire Torch).
    Beschreibungsbasiert - patch-stabil gegen umbenannte Passiven."""
    entry = by_name().get(item_name)
    return bool(entry) and bool(_BURN_RE.search(entry[1].get("description", "")))


def is_lethality(item_name: str) -> bool:
    """True, wenn die Data-Dragon-Beschreibung "Lethality" als Stat nennt (flache
    Ruestungsdurchdringung -> Burst gegen Squishies, NICHT gegen Tanks). Youmuu's,
    The Collector, Hubris, Prowler's u.a. tragen die Stat-Zeile "... Lethality".
    Abgrenzung zu %-Pen-Items (Lord Dominik's/Serylda's/Void Staff): die nennen
    kein "Lethality" im Text."""
    entry = by_name().get(item_name)
    return bool(entry) and "Lethality" in entry[1].get("description", "")


def has_heal_shield_power(item_name: str) -> bool:
    """True, wenn die Data-Dragon-Beschreibung "Heal and Shield Power" als Stat
    nennt. Datengetriebener Marker fuer Heal/Shield-Enchanter (T4b-Support-
    Framing): alle klassischen Enchanter tragen diesen Stat, die Tags allein
    (Health/SpellDamage/...) verraten ihn nicht."""
    entry = by_name().get(item_name)
    return bool(entry) and "Heal and Shield Power" in entry[1].get("description", "")


def tags_of(item_name: str) -> set[str]:
    """Tags eines Items als Menge (leere Menge bei unbekanntem Item). Ersetzt das
    zuvor ~8-fach wiederholte by_name().get(...)-Idiom (Befund D6, Struktur-
    Review 2026-07-17)."""
    entry = by_name().get(item_name)
    return set(entry[1].get("tags", [])) if entry else set()


def conflicts(candidate: str, owned_names: set[str]) -> str | None:
    """Name der geteilten Passive, wenn candidate mit einem FERTIGEN
    Besitz-Item kollidiert. Komponenten (z.B. Sheen) blockieren nicht,
    weil das Kandidaten-Item daraus gebaut werden kann."""
    cand = passive_names().get(candidate)
    if not cand:
        return None
    for owned in owned_names:
        entry = by_name().get(owned)
        if not entry or entry[1].get("into"):  # Komponente -> kein Konflikt
            continue
        shared = cand & passive_names().get(owned, frozenset())
        if shared:
            return sorted(shared)[0]
    return None


def build_discount(item_name: str, owned_ids: list[int]) -> int:
    """Gold-Wert der Inventar-Teilitems, die ins Rezept eingehen
    (0 = keine Ueberschneidung mit dem Inventar).

    Wie im Shop: jedes Inventar-Item deckt hoechstens einen Rezept-Platz ab
    (zwei Long Swords im Rezept brauchen zwei im Inventar). Verschachtelte
    Rezepte werden rekursiv geprueft."""
    entry = by_name().get(item_name)
    if not entry:
        return 0
    return _component_discount(entry[1], Counter(str(i) for i in owned_ids))


def remaining_cost(item_name: str, owned_ids: list[int]) -> int:
    """Gold, das zum Fertigbauen noch fehlt: voller Preis minus Wert der
    Teilitems im Inventar, die in die Rezeptur eingehen."""
    entry = by_name().get(item_name)
    if not entry:
        return 0
    total = entry[1].get("gold", {}).get("total", 0)
    return max(0, total - build_discount(item_name, owned_ids))


def direct_components(item_name: str, owned_ids: list[int]) -> tuple[list[dict], int]:
    """Direkte Rezept-Komponenten des Ziel-Items mit Besitz-Abgleich (Feature 001,
    Kaufplan-Leiste). Rueckgabe: (missing, combine_cost).

    - `missing`: die noch NICHT direkt besessenen direkten Komponenten in
      Rezept-Reihenfolge (Data-Dragon `from`), je Eintrag
      {id, name, cost, remaining}. `cost` ist der volle Komponentenpreis
      (Anzeige/Tooltip), `remaining` der tatsaechliche Restpreis nach Anrechnung
      besessener Unterkomponenten (fuer die kumulative Gold-Rechnung). Bereits
      direkt besessene Komponenten werden ausgelassen; Mehrfachbesitz wird - wie
      bei build_discount - je Rezept-Platz genau einmal verbraucht.
    - `combine_cost`: reine Kombinationsgebuehr (voller Preis minus Summe der
      direkten Komponenten-Vollpreise). So gilt exakt:
      remaining_cost == combine_cost + sum(m["remaining"] for m in missing) -
      die kumulative Rechnung der Leiste zaehlt kein Gold doppelt.
    """
    entry = by_name().get(item_name)
    if not entry:
        return [], 0
    item = entry[1]
    inv = Counter(str(i) for i in owned_ids)
    full_total = item.get("gold", {}).get("total", 0)
    comp_sum = 0
    missing: list[dict] = []
    for comp_id in item.get("from", []):
        comp = by_id(int(comp_id))
        if not comp:
            continue
        cost = comp.get("gold", {}).get("total", 0)
        comp_sum += cost
        if inv[comp_id] > 0:            # Komponente direkt im Inventar -> gedeckt
            inv[comp_id] -= 1
            continue
        saved, _ = _component_match(comp, inv)   # besessene Unterkomponenten anrechnen
        missing.append({"id": int(comp_id), "name": comp["name"],
                        "cost": cost, "remaining": max(0, cost - saved)})
    return missing, max(0, full_total - comp_sum)


# Belegen KEINEN der 6 regulaeren Item-Slots: Boots haben seit Season 2026
# einen eigenen Slot, das Trinket ebenso. Consumables (Pots, Elixiere,
# Control Wards) belegen dagegen sehr wohl einen regulaeren Slot!
_NON_SLOT_TAGS = {"Boots", "Trinket"}

# Season-2026-UTILITY-Role-Quest: der Quest-Slot wird zum Control-Ward-Slot
# (ein Control Ward liegt dort gratis, kostet also KEINEN regulaeren Slot).
_CONTROL_WARD_ID = 2055


def slot_items(owned_ids: list[int], role: str | None = None) -> list[tuple[int, dict]]:
    """Die Inventar-Items, die einen der 6 regulaeren Slots belegen.

    Season-2026-Role-Quest: nur BOTTOM hat einen Gratis-Boots-Slot (der
    Quest-Slot wird zum Boots-Slot). Fuer TOP/JUNGLE/MIDDLE/UTILITY belegen
    Boots einen regulaeren Slot. UTILITY bekommt statt dessen einen Gratis-
    Control-Ward-Slot (Quest-Slot wird zum Ward-Slot) -> ein Control Ward
    zaehlt dort nicht gegen die 6 Slots. `role` steuert beides; ohne `role`
    (Default) bleiben nur Boots slot-frei wie bisher (Rueckwaertskompatibilitaet).
    Das Trinket ist immer slot-frei."""
    boots_are_free = role is None or role == "BOTTOM"
    ward_is_free = role == "UTILITY"
    result = []
    for item_id in owned_ids:
        item = by_id(item_id)
        if not item:
            continue
        tags = set(item.get("tags", []))
        if "Trinket" in tags:
            continue
        if "Boots" in tags and boots_are_free:
            continue
        if ward_is_free and item_id == _CONTROL_WARD_ID:
            continue
        result.append((item_id, item))
    return result


def _component_match(item: dict, inventory: Counter) -> tuple[int, list[str]]:
    """Rekursiv: (angerechnetes Gold, Namen der verbrauchten Inventar-Teilitems).
    Jedes Inventar-Item deckt hoechstens einen Rezept-Platz ab."""
    saved, names = 0, []
    for comp_id in item.get("from", []):
        comp = by_id(int(comp_id))
        if not comp:
            continue
        if inventory[comp_id] > 0:
            inventory[comp_id] -= 1
            saved += comp.get("gold", {}).get("total", 0)
            names.append(comp["name"])
        else:
            s, n = _component_match(comp, inventory)
            saved += s
            names += n
    return saved, names


def _component_discount(item: dict, inventory: Counter) -> int:
    return _component_match(item, inventory)[0]


def _finished_targets(owned_ids: list[int]) -> set[str]:
    """Fertige Items (kein 'into'), zu denen die Inventar-Komponenten fuehren."""
    targets: set[str] = set()
    for cid in {str(i) for i in owned_ids}:
        item = by_id(int(cid))
        if not item:
            continue
        stack, seen = list(item.get("into", [])), set()
        while stack:
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            fitem = by_id(int(fid))
            if not fitem:
                continue
            if fitem.get("into"):
                stack.extend(fitem["into"])
            else:
                targets.add(fid)
    return targets


# Erst ab dieser Komponenten-Groesse gilt ein Kauf als Absichts-Signal:
# alles bis einschliesslich Pickaxe-Preis (875 G) steckt in so vielen
# Rezepten, dass es nichts ueber das Ziel-Item verraet.
MIN_SIGNAL_GOLD = 876


def _has_signal_component(comp_names: list[str]) -> bool:
    lookup = by_name()
    return any(
        (entry := lookup.get(n)) is not None
        and entry[1].get("gold", {}).get("total", 0) >= MIN_SIGNAL_GOLD
        for n in comp_names
    )


def in_progress(owned_ids: list[int], kb_names=frozenset()) -> dict | None:
    """Erkennt das Item, das der Spieler laut seinen Komponenten gerade baut.

    Rein aus den Rezepten (Data Dragon), unabhaengig von der Wissensbasis -
    denn genau die halbfertigen Items sind das staerkste Absichts-Signal.
    Rueckgabe: bestes Kandidaten-Item {item, remaining, invested, components,
    meta} oder None. Konfidenz: mindestens eine Komponente >= MIN_SIGNAL_GOLD
    (alles bis inkl. Pickaxe ist zu mehrdeutig) UND (>=2 investierte
    Komponenten ODER Item steht im Build der Rolle/kb_names) - so raet weder
    eine einzelne Pickaxe noch ein Long Sword etwas Falsches.
    """
    inv = Counter(str(i) for i in owned_ids)
    best_key, best = None, None
    for fid in _finished_targets(owned_ids):
        if fid in inv:            # schon fertig im Inventar
            continue
        fitem = by_id(int(fid))
        if not fitem:
            continue
        invested, comps = _component_match(fitem, inv.copy())
        if invested <= 0:
            continue
        meta = fitem["name"] in kb_names
        if len(comps) < 2 and not meta:
            continue
        if not _has_signal_component(comps):
            continue
        remaining = max(0, fitem.get("gold", {}).get("total", 0) - invested)
        key = (len(comps), invested, -remaining)   # meiste Investition zuerst
        if best_key is None or key > best_key:
            best_key = key
            best = {"item": fitem["name"], "remaining": remaining,
                    "invested": invested, "components": comps, "meta": meta}
    return best


def is_completed(item_id: int) -> bool:
    """True fuer ein FERTIGES Item (nicht Komponente): kein 'into', >= 2000 G,
    auf Summoner's Rift, keine Boots/Trinkets/Ally-Only-Items. Spiegelt die
    `completed`-Klassifikation der Pipeline (aggregate.classify_items) - Basis
    fuer die Spike-Warnung (Review Befund 4.2).

    Sonderfall Support-Item: die fertigen Quest-Endformen (COMPLETED_SUPPORT_ITEMS)
    zaehlen hier bewusst ZUSAETZLICH als fertig, obwohl sie nur 400 G kosten - sonst
    unterzaehlt die Live-Spike-Warnung den Support um 1. Das betrifft NUR diese
    App-Funktion; die Pipeline-Klassifikation (aggregate.classify_items) bleibt
    unberuehrt."""
    item = by_id(item_id)
    if not item or item.get("into") or item.get("requiredAlly"):
        return False
    if set(item.get("tags", [])) & _NON_SLOT_TAGS:  # Boots/Trinket
        return False
    if not item.get("maps", {}).get("11"):
        return False
    if item.get("name") in COMPLETED_SUPPORT_ITEMS:  # Support-Endform (400 G)
        return True
    return item.get("gold", {}).get("total", 0) >= 2000


def count_completed(item_ids: list[int]) -> int:
    """Anzahl fertiger Items im Inventar (Komponenten zaehlen NICHT mit)."""
    return sum(1 for i in item_ids if is_completed(i))


def is_upgraded_boots(item_name: str) -> bool:
    """True fuer ein "echtes" Boots-Upgrade (T2 oder hoeher), False fuer die
    300-G-Basis-Boots (Item 1001 "Boots", 2422 "Slightly Magical Footwear").

    WARUM: Die Basis-Boots tragen selbst den Tag "Boots" und werden praktisch
    immer frueh gekauft. Ohne diese Unterscheidung wuerde ihr Besitz die
    Boots-Empfehlung dauerhaft unterdruecken - der Spieler bekaeme nie ein
    Boots-Upgrade vorgeschlagen. Kriterium: Tag "Boots" UND gold.total >= 900 -
    dieselbe Schwelle wie aggregate.classify_items, wo Boots erst ab 900 G in
    die Wissensbasis zaehlen."""
    entry = by_name().get(item_name)
    if not entry:
        return False
    item = entry[1]
    return ("Boots" in item.get("tags", [])
            and item.get("gold", {}).get("total", 0) >= 900)


def is_valid_sr(name: str) -> bool:
    """True, wenn das Item im AKTUELLEN Data Dragon existiert und auf Summoner's
    Rift gueltig ist (maps['11']). Filtert entfernte/Legacy- und Nicht-SR-Items
    (Arena/ARAM-only) aus den Empfehlungen (Scope: SR 5v5)."""
    entry = by_name().get(name)
    return bool(entry and entry[1].get("maps", {}).get("11"))


def is_tenacity_boots(item_name: str) -> bool:
    """True fuer Tenacity-Boots (Mercury's Treads und ihr T3-Upgrade): Boots-Tag
    UND "Tenacity" im Beschreibungstext. Robust ueber die Beschreibung statt ueber
    den Anzeigenamen - so faellt auch ein Upgrade/eine Umbenennung nicht durchs
    Raster. (Live-Referenz-IDs zur Sicherheit: 3111 T2, 3173 T3.)"""
    entry = by_name().get(item_name)
    if not entry:
        return False
    item = entry[1]
    return ("Boots" in item.get("tags", [])
            and "Tenacity" in item.get("description", ""))


def standard_defensive_boots(vs: str) -> str | None:
    """Kanonische T2-Defensiv-Boots gegen einen Schadenstyp, unabhaengig von
    der (evtl. duennen) champion-spezifischen Boots-Liste: 'ad' -> T2-Boots
    mit Armor-Tag (Plated Steelcaps), 'ap' -> mit SpellBlock (Mercury's Treads).
    Bevorzugt die aufwertbare T2-Stufe (hat 'into') vor der T3-Stufe. None,
    wenn nichts passt (z.B. Statik fehlt)."""
    tag = "Armor" if vs == "ad" else "SpellBlock"
    fallback = None
    for name, (_item_id, item) in by_name().items():
        tags = item.get("tags", [])
        if "Boots" not in tags or tag not in tags:
            continue
        if item.get("gold", {}).get("total", 0) < 900:
            continue
        if item.get("into"):
            return name  # T2 (aufwertbar) -> sofort zurueck
        if fallback is None:
            fallback = name
    return fallback


def categorize_gold(item_ids: list[int]) -> dict:
    """Verteilt den Gold-Wert der Items auf die Kategorien ad/ap/defense.

    Ein Item kann mehrere Kategorien treffen; sein Gold wird dann anteilig
    aufgeteilt. Rueckgabe enthaelt auch das Gesamt-Gold und die Crit-Item-Zahl.
    """
    buckets = {"ad": 0.0, "ap": 0.0, "defense": 0.0}
    total = 0
    crit_items = 0
    for item_id in item_ids:
        item = by_id(item_id)
        if not item:
            continue
        gold = item.get("gold", {}).get("total", 0)
        total += gold
        tags = set(item.get("tags", []))
        if "CriticalStrike" in tags:
            crit_items += 1
        hits = [key for key, group in
                (("ad", AD_TAGS), ("ap", AP_TAGS), ("defense", DEF_TAGS))
                if tags & group]
        for key in hits:
            buckets[key] += gold / len(hits)
    return {"buckets": buckets, "gold_total": total, "crit_items": crit_items}
