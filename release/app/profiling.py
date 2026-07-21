"""Gegner-Bedrohungsprofile aus Live-Items ableiten.

Pro Gegner: ausgegebenes Gold, Schadensprofil (AD/AP-Anteil), Build-
Klassifikation (tank/crit_dps/burst_ad/burst_ap/hybrid) und Threat-Score.
"""

from functools import lru_cache

from . import champions, items

PRIORITY_WEIGHT = {"low": 0.5, "medium": 1.0, "high": 1.5, "urgent": 2.0}


@lru_cache(maxsize=1)
def _canonical_ids() -> dict:
    """Vereinfachte Champion-ID -> kanonische Data-Dragon-ID. Faengt das
    Riot-interne Casing ab ('FiddleSticks' in Live/Match-V5 vs. 'Fiddlesticks'
    in Data Dragon) - an der Wurzel, damit alle exakt vergleichenden Lookups
    (recommend._HEALING_THREATS, champions.prior_for_id/bucket_for_id) treffen."""
    from pipeline import ddragon
    from pipeline.config import Config
    from pipeline.ddragon import _simplify
    version = ddragon.latest_version()
    data = ddragon.champions(version, Config.load().cache_dir)["data"]
    return {_simplify(info["id"]): info["id"] for info in data.values()}


def _canonicalize_id(cid: str) -> str:
    """Roh-ID gegen die bekannten Data-Dragon-IDs kanonisieren; trifft der
    vereinfachte Vergleich nichts (unbekannter Champion / kein Cache), bleibt die
    Roh-ID unveraendert (Fallback-Verhalten)."""
    from pipeline.ddragon import _simplify
    try:
        return _canonical_ids().get(_simplify(cid), cid)
    except Exception:
        return cid

# Gleitende Mischung Champion-Prior <-> Live-Item-Split fuer den Schadenstyp.
# Das Gewicht des Item-Splits waechst linear mit dem klassifizierten
# OFFENSIV-Gold (ad+ap), nicht mit dem Gesamt-Gold: Ein Full-Tank hat kaum
# Offensiv-Gold, also bleibt bei ihm der Prior dominant (Malphite-Fall aus dem
# Review). Erst mit echten Schadensitems verschiebt sich der Split Richtung
# Build - so werden Abweichungen (AP-Malphite, Crit-Sion) erkannt, ohne im
# Fruehspiel blind zu sein.
PRIOR_ONLY_BELOW_GOLD = 1500   # bis hierher reiner Prior (kaum Item-Aussage)
ITEM_FULL_WEIGHT_GOLD = 8000   # ab hier dominiert der Item-Split vollstaendig


def verified_champion_id(player: dict) -> str | None:
    """Verifizierte Data-Dragon-ID aus `rawChampionName` (Befund A, 2026-07-15).

    Der alte Parser nahm an, das Feld habe IMMER die Form
    'game_character_displayname_<Id>' und schnitt hinter dem letzten '_'. Live
    existiert aber mindestens ein zweites Format:
    'Character_Seraphine_Name' -> der Teil hinter dem letzten '_' waere 'Name',
    fuer das es keinen Prior gibt (Schadenstyp faellt faelschlich auf 0.5/0.5).

    Statt einer zweiten Rateregel wird jeder Token des rawChampionName von
    RECHTS nach LINKS gegen den Data-Dragon-Championkatalog VERIFIZIERT (DD-IDs
    enthalten nie '_'); der erste Treffer gewinnt und wird als kanonische DD-ID
    zurueckgegeben. Beispiele:
      - 'game_character_displayname_Neeko' -> 'Neeko' (letztes Token trifft)
      - 'Character_Seraphine_Name'         -> 'Name' scheitert, 'Seraphine' trifft
    Trifft kein Token, wird der Anzeigename (championName) aufgeloest.

    Rueckgabe None NUR, wenn der Katalog erfolgreich konsultiert wurde und weder
    ein Token noch der Anzeigename aufloesbar war (z.B. Minion-Eintraege wie
    'game_character_displayname_Red_Minion_Basic' - Befund B). Schlaegt der
    Katalog-Zugriff selbst fehl (kein Cache / offline), greift das alte
    Verhalten als Fallback und es kommt NIE None zurueck (sonst wuerde der
    Server jeden Spieler verwerfen)."""
    from pipeline.ddragon import _simplify
    raw = player.get("rawChampionName") or ""
    display = player.get("championName", "?")
    try:
        catalog = _canonical_ids()
    except Exception:
        # Offline-Guard: altes Verhalten, niemals None.
        if raw:
            cid = raw.rsplit("_", 1)[-1]
            if cid:
                return _canonicalize_id(cid)
        return champions.resolve_id(display) or display
    for token in reversed([t for t in raw.split("_") if t]):
        hit = catalog.get(_simplify(token))
        if hit:
            return hit
    return champions.resolve_id(display) or None


def champion_id_of(player: dict) -> str:
    """Stabile Data-Dragon-ID des Spielers (Fix 5.7 + Befund A 2026-07-15).
    Primaerquelle ist `rawChampionName` der Live-Client-API. Statt der frueheren
    Formatannahme (Teil hinter dem letzten '_') werden die Tokens jetzt gegen den
    Data-Dragon-Katalog verifiziert (siehe `verified_champion_id`) - so faengt
    der Parser auch Fremdformate wie 'Character_Seraphine_Name' ab. Ist der
    Champion nicht auflösbar, bleibt der Anzeigename als Fallback (Vertrag:
    niemals None; wird u.a. vom Backtest ueber profile_player genutzt)."""
    cid = verified_champion_id(player)
    if cid is not None:
        return cid
    display = player.get("championName", "?")
    return champions.resolve_id(display) or display


def _damage_split(champion_id: str, buckets: dict) -> dict:
    """Kombinierter AD/AP-Schadenstyp: Champion-Prior als Basis, Live-Items als
    gleitende Verfeinerung. Nie mehr 0/0 - ab Minute 0 gefuellt."""
    prior = champions.prior_for_id(champion_id)
    offense = buckets["ad"] + buckets["ap"]
    if offense <= 0:
        ad = prior["ad"]
    else:
        item_ad = buckets["ad"] / offense
        # Item-Gewicht linear zwischen den beiden Schwellen (geklemmt 0..1).
        span = ITEM_FULL_WEIGHT_GOLD - PRIOR_ONLY_BELOW_GOLD
        w_item = max(0.0, min(1.0, (offense - PRIOR_ONLY_BELOW_GOLD) / span))
        ad = (1.0 - w_item) * prior["ad"] + w_item * item_ad
    return {"ad": round(ad, 2), "ap": round(1.0 - ad, 2)}


def _has_smite(player: dict) -> bool:
    """True, wenn der Spieler Smite in den summonerSpells traegt (Jungle-Signal).
    Robust ueber rawDisplayName/rawDescription (locale-unabhaengig, enthaelt
    'SummonerSmite'), displayName nur als Fallback."""
    for spell in player.get("summonerSpells", {}).values():
        if not isinstance(spell, dict):
            continue
        raw = (f"{spell.get('rawDisplayName', '')} {spell.get('rawDescription', '')} "
               f"{spell.get('displayName', '')}")
        if "smite" in raw.lower():
            return True
    return False


def profile_player(player: dict, champion_id: str | None = None,
                   name: str | None = None) -> dict:
    """Bedrohungsprofil eines Spielers. `champion_id` und `name` koennen von
    aussen vorgegeben werden (Befund B 2026-07-15: der Server pinnt die Identität
    pro Spiel an der riotId, damit Neekos Verwandlungen das Profil nicht
    umschreiben). Ohne die Argumente exakt das bisherige Verhalten - champion_id
    kommt dann aus champion_id_of, name aus championName (Backtest unberuehrt)."""
    item_ids = [entry["itemID"] for entry in player.get("items", [])]
    cat = items.categorize_gold(item_ids)
    buckets, gold_total = cat["buckets"], cat["gold_total"]
    offense = buckets["ad"] + buckets["ap"]
    classified = offense + buckets["defense"]

    # build_profile bleibt bewusst rein item-basiert ("WIE baut er"): Bis genug
    # Item-Gold da ist, ist die Bauweise nicht bestimmbar -> "unknown". Der
    # Schadenstyp (WELCHEN Schaden) kommt dagegen aus _damage_split (Prior).
    if gold_total < 1500 or classified == 0:
        build = "unknown"
    else:
        ad_share = buckets["ad"] / classified
        ap_share = buckets["ap"] / classified
        def_share = buckets["defense"] / classified
        if def_share >= 0.55:
            build = "tank"
        elif cat["crit_items"] >= 2:
            build = "crit_dps"
        elif ad_share > ap_share:
            build = "burst_ad" if ad_share >= 0.6 else "hybrid"
        else:
            build = "burst_ap" if ap_share >= 0.6 else "hybrid"

    champion_id = champion_id if champion_id is not None else champion_id_of(player)
    damage_split = _damage_split(champion_id, buckets)

    scores = player.get("scores", {})
    kda = (scores.get("kills", 0) + scores.get("assists", 0)) / max(1, scores.get("deaths", 0))
    return {
        "name": name if name is not None else player.get("championName", "?"),
        "champion_id": champion_id,
        "summoner": player.get("riotIdGameName") or player.get("summonerName", ""),
        "level": player.get("level", 0),
        # Smite im Spell-Setup: staerkstes Rollen-Signal fuer den Gegen-Jungler
        # (rec_stance._counterpart). Locale-unabhaengig ueber rawDisplayName/
        # rawDescription, displayName nur als Fallback.
        "has_smite": _has_smite(player),
        "gold_spent": gold_total,
        "completed_items": items.count_completed(item_ids),
        "damage_split": damage_split,
        "build_profile": build,
        "kda": round(kda, 1),
        "scores": {k: scores.get(k, 0) for k in ("kills", "deaths", "assists", "creepScore")},
        "items": [items.name_of(i) for i in item_ids],
    }


# Assists zaehlen im Threat-Score weniger als Kills (Fix 5.2): sonst wird ein
# 0/2/12-Enchanter faelschlich High-Threat. Kills belegen echten Carry-Druck,
# Assists nur Beteiligung.
THREAT_ASSIST_WEIGHT = 0.4


def add_threat_scores(profiles: list[dict], priorities: dict[str, str]) -> None:
    """Zwei entkoppelte Werte je Gegner (Fix 5.2):

    - `threat_score`: der ROHE, ungewichtete Bedrohungswert aus Gold (relativ
      zum reichsten Gegner) und kill-gewichteter KDA. Das ist der Wert, auf dem
      die Stance-Logik (recommend.own_stance/top_threat) rechnet - eine manuelle
      Priority darf die Stance NICHT mehr verschieben.
    - `display_score`: derselbe Wert mal dem manuellen Priority-Faktor, nur fuer
      das Anzeige-Ranking der Threat-Karten (server sortiert danach).
    - `threat_share`: prozentualer Anteil an der Team-Bedrohung (0..1),
      normalisiert ueber alle Gegner auf Basis von `display_score` (also inkl.
      Prio-Faktor - damit stimmen angezeigte Zahl und Karten-Sortierung
      ueberein). Ein gefedeter Gegner dominiert, urgent-Prio hebt seinen Anteil.
    """
    max_gold = max((p["gold_spent"] for p in profiles), default=0) or 1
    for p in profiles:
        gold_norm = p["gold_spent"] / max_gold
        sc = p.get("scores", {})
        # Kill-gewichtete KDA statt Kills+Assists gleichgewichtet.
        threat_kda = ((sc.get("kills", 0) + THREAT_ASSIST_WEIGHT * sc.get("assists", 0))
                      / max(1, sc.get("deaths", 0)))
        kda_norm = min(threat_kda / 5.0, 1.0)
        score = 0.55 * gold_norm + 0.45 * kda_norm
        weight = PRIORITY_WEIGHT.get(priorities.get(p["name"], "medium"), 1.0)
        p["priority"] = priorities.get(p["name"], "medium")
        p["threat_score"] = round(score, 2)
        p["display_score"] = round(min(score * weight, 1.0), 2)
    # Anteil an der Team-Bedrohung (Anzeige): display_score normalisiert. Summe 0
    # -> Gleichverteilung (1/n), leere Liste crasht nicht.
    n = len(profiles)
    total = sum(p["display_score"] for p in profiles)
    for p in profiles:
        share = (p["display_score"] / total) if total > 0 else (1.0 / n)
        p["threat_share"] = round(share, 2)


# --- Absolutes Fed-Signal (Review-Befund E/F, 2026-07-13) -----------------
# Der relative threat_score oben ist eine RANGORDNUNG (reichster Gegner = 1.0),
# kein Fed-Mass: in einem ausgeglichenen Spiel gilt der beste Gegner fast immer
# als "fed". Fuer Schwellen-Entscheidungen (Stance defensiv / Anti-Heal lohnt)
# braucht es ein ABSOLUTES Signal - Gold/Item-Vorsprung gegenueber der
# zeitabhaengigen Erwartung plus Netto-Kills.

# Empirische Median-Kurve des Ausgabegolds (Befund C, review-2026-07-15.md):
# 12.330 Timelines Patch 16.13, ~4,3 Mio. Snapshots, Messgroesse identisch zur
# Live-App (Summe gold.total des Inventars). Die alte lineare Naeherung
# (390 G/min - 600, Fit nur bis Min 32) ueberschoss den Median durchgaengig um
# 300-850 G und lief ab ~Min 40 dem Full-Build-Deckel davon (Min 48: 18510 vs.
# real 16750) - spaet war dadurch praktisch nie jemand "fed".
# Neu rechnen: tmp/calibrate_expected_gold.py.
_GOLD_MEDIAN = (
    (3, 450), (5, 1050), (7, 1650), (9, 2300), (11, 3000), (13, 3700),
    (15, 4400), (17, 5200), (19, 6050), (21, 6900), (23, 7800), (25, 8600),
    (27, 9450), (29, 10350), (31, 11100), (33, 11850), (35, 12700),
    (37, 13450), (39, 14100), (41, 14650), (43, 15450), (45, 16100),
    (47, 16450), (49, 16750), (51, 16900),
)
GOLD_FLOOR = 400.0         # Sockel: sehr frueh (Loading, t=0) bleibt die
                           # Erwartung positiv -> niemand faelschlich "fed".
# Fertige Items je Zeit: ab ~Minute 4 rund ein fertiges Item alle 8 Minuten
# (Median-Verlauf: 1 Item ~Min 12, 2 ~Min 20, 3 ~Min 28). Gedeckelt bei 5, weil
# der Median fertiger Items ab ~Min 42 bei 5 saettigt (der 6. Slot sind Boots,
# die nicht als "completed" zaehlen).
ITEMS_START_MIN = 4.0
ITEMS_PER_MIN = 1.0 / 8.0
ITEMS_CAP = 5.0

# Stufe STARK FED (ersetzt top_threat >= 0.8 in der Stance): der Gegner liegt
# klar ueber der Erwartung. Entweder Kill- UND Gold-Vorsprung zusammen, ein
# dominanter Solo-Kill-Vorsprung, oder klarer Item- plus Gold-Vorsprung.
STRONG_GOLD_FRAC = 1.25       # >= 125% des Median-Golds (ueber p75)
STRONG_NET_KILLS = 3          # zusammen mit Gold-Vorsprung
STRONG_NET_KILLS_SOLO = 6     # dominanter Kill-Vorsprung allein
STRONG_ITEM_LEAD = 2          # >= 2 fertige Items ueber der Erwartung
STRONG_GOLD_FRAC_ITEM = 1.10  # zum Item-Vorsprung genuegt leichter Gold-Vorsprung

# Stufe GEFEDET GENUG FUER ANTI-HEAL (ersetzt threat_score >= 0.6 / Team-Median):
# schwaechere Schwelle - >= 1 fertiges Item UND Gold ueber der Erwartung. Im
# Zweifel NICHT triggern (Anti-Heal ist Option, kein Pflichtkauf).
WEAK_GOLD_FRAC = 1.10         # >= 110% des Median-Golds
WEAK_NET_KILLS = 0            # nicht im Minus (kein 0/5-Champ)

# Hysterese (Schmitt-Trigger, Befund C): Ausloesen und Loslassen brauchen
# verschiedene Schwellen. Die Erwartung waechst auch, waehrend ein Gegner gerade
# nichts kauft - ohne Hysterese blinkt das Flag an/aus. Einmal fed, bleibt der
# Zustand, solange das Gold noch ueber der Release-Schwelle liegt.
STRONG_RELEASE_FRAC = 1.10   # einmal stark fed -> bleibt es, solange Gold >= 110% der Erwartung
WEAK_RELEASE_FRAC = 1.00     # einmal fed genug -> bleibt es, solange Gold >= der Erwartung


def expected_gold(game_time: float) -> float:
    """Zeitabhaengiges Median-Ausgabegold eines Spielers (empirisch, Befund C).

    game_time in Sekunden. Lineare Interpolation ueber die gemessene Median-
    Tabelle `_GOLD_MEDIAN` (Summe gold.total des Inventars, identisch zur Live-
    Metrik). Unterhalb des ersten Stuetzpunkts auf dessen Wert geklemmt, oberhalb
    des letzten FLACH weitergefuehrt (16.900) - NICHT linear extrapoliert: das
    Ausgabegold saettigt gegen ~17.000 (Full-Build-Deckel), eine lineare
    Verlaengerung liefe dem realen Maximum davon und machte spaete Fed-Gegner
    faelschlich "nicht fed" (Vladimir-Fall). Unterhalb des ersten Stuetzpunkts
    (Min 3) wird vom Sockel GOLD_FLOOR bei t=0 zum ersten Median-Wert
    interpoliert, damit Minute 0 / Loading niemanden als 'fed' markiert."""
    minutes = max(0.0, game_time) / 60.0
    first_min, first_gold = _GOLD_MEDIAN[0]
    last_min, last_gold = _GOLD_MEDIAN[-1]
    if minutes <= 0.0:
        median = GOLD_FLOOR
    elif minutes <= first_min:
        # Frueh-Rampe: GOLD_FLOOR (t=0) -> erster Stuetzpunkt.
        frac = minutes / first_min
        median = GOLD_FLOOR + frac * (first_gold - GOLD_FLOOR)
    elif minutes >= last_min:
        median = float(last_gold)
    else:
        median = float(last_gold)
        for (m0, g0), (m1, g1) in zip(_GOLD_MEDIAN, _GOLD_MEDIAN[1:]):
            if m0 <= minutes <= m1:
                frac = (minutes - m0) / (m1 - m0)
                median = g0 + frac * (g1 - g0)
                break
    return max(GOLD_FLOOR, median)


def expected_items(game_time: float) -> float:
    """Zeitabhaengige erwartete Zahl fertiger Items (Median-Verlauf), gedeckelt
    bei ITEMS_CAP=5: der Median fertiger Items saettigt ab ~Min 42 bei 5 (der 6.
    Slot sind Boots, die nicht als "completed" zaehlen)."""
    minutes = max(0.0, game_time) / 60.0
    return min(max(0.0, (minutes - ITEMS_START_MIN) * ITEMS_PER_MIN), ITEMS_CAP)


def _net_kills(profile: dict) -> int:
    sc = profile.get("scores", {})
    return sc.get("kills", 0) - sc.get("deaths", 0)


def is_strongly_fed(profile: dict, game_time: float) -> bool:
    """Gegner ist ABSOLUT stark fed (Stance-Schwelle): deutlich ueber der
    zeitabhaengigen Erwartung aus Gold, fertigen Items und Netto-Kills.

    Hysterese (Befund C): Ist keine der Trigger-Bedingungen erfuellt, das Profil
    trug aber im Vorschritt schon 'stark fed' (`fed_prev_strong` truthy), gilt es
    weiterhin als stark fed, solange `gold_frac >= STRONG_RELEASE_FRAC`. Grund:
    Die Erwartung waechst auch, waehrend der Gegner gerade nichts kauft - ohne
    diese getrennte Loslass-Schwelle blinkt das Flag an/aus. Ohne das Feld
    `fed_prev_strong` bleibt die Funktion rein/zustandslos (Backtest)."""
    exp = expected_gold(game_time)
    gold_frac = profile.get("gold_spent", 0) / exp if exp > 0 else 0.0
    net = _net_kills(profile)
    item_lead = profile.get("completed_items", 0) - expected_items(game_time)
    triggered = ((net >= STRONG_NET_KILLS and gold_frac >= STRONG_GOLD_FRAC)
                 or net >= STRONG_NET_KILLS_SOLO
                 or (item_lead >= STRONG_ITEM_LEAD and gold_frac >= STRONG_GOLD_FRAC_ITEM))
    if triggered:
        return True
    if profile.get("fed_prev_strong") and gold_frac >= STRONG_RELEASE_FRAC:
        return True
    return False


def is_fed_enough(profile: dict, game_time: float) -> bool:
    """Gegner ist gefedet genug, dass Anti-Heal lohnt (schwache Schwelle):
    mindestens ein fertiges Item UND Gold ueber der Erwartung, nicht im
    Kill-Minus. Im Zweifel False (Anti-Heal ist Option, kein Pflichtkauf).

    Hysterese (Befund C): Ist die Trigger-Bedingung nicht erfuellt, das Profil
    trug aber im Vorschritt schon 'fed genug' (`fed_prev_weak` truthy), haelt der
    Zustand, solange `completed_items >= 1 UND gold_frac >= WEAK_RELEASE_FRAC`.
    Die Netto-Kill-Bedingung wird beim Halten BEWUSST ignoriert - die Kill-Bilanz
    schwankt spaet stark; Items/Gold bleiben der Beleg, ein 5-Item-Vladimir wird
    durch zwei spaete Tode nicht harmlos. Ohne das Feld `fed_prev_weak` bleibt die
    Funktion rein/zustandslos (Backtest)."""
    exp = expected_gold(game_time)
    gold = profile.get("gold_spent", 0)
    completed = profile.get("completed_items", 0)
    triggered = (completed >= 1
                 and gold >= exp * WEAK_GOLD_FRAC
                 and _net_kills(profile) >= WEAK_NET_KILLS)
    if triggered:
        return True
    gold_frac = gold / exp if exp > 0 else 0.0
    if profile.get("fed_prev_weak") and completed >= 1 and gold_frac >= WEAK_RELEASE_FRAC:
        return True
    return False


def any_strongly_fed(profiles: list[dict], game_time: float) -> bool:
    """True, wenn mindestens ein Gegner absolut stark fed ist (Stance)."""
    return any(is_strongly_fed(p, game_time) for p in profiles)


def team_damage_split(profiles: list[dict]) -> dict:
    """Threat-gewichteter AD/AP-Anteil des Gegnerteams."""
    total_ad = total_ap = 0.0
    for p in profiles:
        weight = p.get("threat_score", 0.5) or 0.1
        total_ad += p["damage_split"]["ad"] * weight
        total_ap += p["damage_split"]["ap"] * weight
    total = total_ad + total_ap
    if total == 0:
        return {"ad": 0.5, "ap": 0.5}
    return {"ad": round(total_ad / total, 2), "ap": round(total_ap / total, 2)}


def team_cc_score(profiles: list[dict]) -> float:
    """CC-Last des Gegnerteams: UNGEWICHTETES Mittel der Champion-CC-Priors
    (cc_per_min). Gegner ohne Prior werden ausgelassen - gleiche Konvention wie
    champions.damage_bucket beim Schadens-Split. Kein Prior im Team -> 0.0."""
    scores = [s for s in (champions.cc_per_min_for_id(p.get("champion_id"))
                          for p in profiles) if s is not None]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def cc_threats(profiles: list[dict], n: int = 2) -> list[str]:
    """Namen der CC-staerksten Gegner (nach cc_per_min-Prior, absteigend), fuer
    die Begruendungstexte. Gegner ohne Prior fallen raus."""
    scored = [(champions.cc_per_min_for_id(p.get("champion_id")),
               p.get("name", "?")) for p in profiles]
    scored = [(s, name) for s, name in scored if s is not None]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [name for _s, name in scored[:n]]
