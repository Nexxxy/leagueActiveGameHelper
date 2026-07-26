"""Empfehlungs-Engine: Wissensbasis + Gegner-Profile + eigene Performance -> Items.

Stance-Logik:
  struggling (schlechte KDA / viele Tode)  -> defensiv
  ahead (gute KDA und Gold-Vorsprung)      -> aggressiv
  sonst                                    -> ausgewogen
"""

from dataclasses import dataclass, field, replace

from core import stats
from . import champions, items, knowledge, profiling
# Fassade (Struktur-Review 2026-07-17 T2): die Themen-Helfer liegen jetzt in
# eigenen Modulen, werden hier aber unter ihren alten Namen re-exportiert. Tests
# und pipeline/backtest.py greifen massiv auf `recommend.<name>` zu (auch auf die
# Unterstrich-Namen und Konstanten) - darum das noqa: F401.
from .rec_stance import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    STATE_LEAD_GOLD,
    fielded_lead, earned_lead, lead_note, own_stance, _stance_note,
)
from .rec_archetype import _select_archetype  # noqa: F401  Fassade (T2)
from .rec_explain import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _bucket_label, explain_item, _item_tag, _is_defensive,
)
from .rec_antiheal import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _my_damage_type, _antiheal_recommendation,
)


@dataclass(frozen=True)
class Weights:
    """Zentrale Stellschrauben fuer das Ranking der situativen Items.

    Der Offline-Backtest schaltet einzelne Schichten ab, indem er das jeweilige
    Gewicht auf 0 setzt (Ablation); kuenftiges Tuning aendert nur noch diese
    Werte.

    Stance-Schicht = REINE ANZEIGE (Review-Befund D, 2026-07-13): Die lage-
    konditionierte Backtest-Auswertung (by_stance) hat gezeigt, dass der
    Stance-Score-Eingriff selbst auf seinem Ziel-Subset (defensive Stance +
    Sieger, n=440) NICHT gewinnt - Hit@3 54,3% (aktiv) vs. 55,5% (aus), Hit@1
    28,6% vs. 40,5% - und die Engine insgesamt unter die Baseline drueckt. Die
    drei Stance-Score-Gewichte stehen darum auf 0 und `stance_next` auf False:
    `own_stance` und `stance`/`stance_reason` (Badge/Text im Frontend) bleiben
    unveraendert, aber die Stance greift NICHT mehr ins Ranking oder in die
    Kaufreihenfolge ein. Das Score-Verhalten ist weiter implementiert und ueber
    explizite Weights(defensive_stance=0.5, defensive_balanced=0.15,
    aggressive_offense=0.25, stance_next=True) reaktivierbar (so testen es die
    Stance-Score-Tests)."""
    defensive_stance: float = 0.0     # defensives Item bei stance == "defensive" (Anzeige-only)
    defensive_balanced: float = 0.0   # defensives Item bei stance == "balanced" (Anzeige-only)
    aggressive_offense: float = 0.0   # nicht-defensives Item bei stance == "aggressive" (Anzeige-only)
    threat_cap: float = 0.2           # Cap des by_threat-Schubs (+/-)
    threat_scale: float = 0.8         # Skalierung des by_threat-Schubs
    state_cap: float = 0.2            # Cap des by_state-Schubs (+/-)
    synergy_factor: float = 0.3       # Faktor fuer _synergy_boost
    redundancy_penalty: float = 0.3   # Abzug bei redundantem Sustain-Stack
    use_archetypes: bool = True       # False -> _select_archetype ueberspringen
    stance_next: bool = False         # True -> _pick_next nimmt Stance-Sonderpfade (Anzeige-only: aus)
    # Botlane-Partner-Layer (T4, research_bot_sup_mates.md 9.40) - wirkt NUR bei
    # role == "UTILITY" mit klassifiziertem Bot-Partner. Als Weights-Felder, damit
    # Backtest/Ablation sie auf 0 setzen kann.
    partner_buff_boost: float = 0.3   # Ally-Buff-Item passend zum Partner (Staff@AP / Ardent@AD)
    partner_buff_penalty: float = 0.4 # Ally-Buff-Item fuer den falschen Partner-Typ (Demotion)
    partner_enchanter_tilt: float = 0.15  # weicher Malus auf Heal/Shield-Enchanter bei AP-Partner
    # KB-datengetriebener Partner-Schub (Phase 2, by_partner in builds.yaml): wirkt
    # NUR bei role==UTILITY + rich + gueltiger Partner-Bucket. Getrennt von den
    # T4-Heuristik-Gewichten (partner_buff_*), damit Ablation unabhaengig moeglich ist.
    partner_kb_cap: float = 0.2       # Cap des by_partner-Schubs (+/-)
    partner_kb_scale: float = 0.8     # Skalierung des by_partner-Schubs


DEFAULT_WEIGHTS = Weights()


# Datenhygiene fuer die empirischen Schuebe (by_threat/by_state). Bewusst NICHT
# in Weights: das sind keine Score-Gewichte, sondern Rausch-Filter - aber als
# Modul-Konstanten leicht tunbar.
# Beta-Prior-Staerke fuer die Shrinkage (Review-Empfehlung 10-20): die
# beobachtete Item-Win-Rate wird Richtung Basisrate des Buckets/Zustands
# gezogen. Kleine Zellen -> nahe Basis (kaum Ranking-Wirkung), grosse Zellen
# (Briar, Shyvana) -> nahe Rohwert.
SHRINK_K = 15
# Ein by_threat-/by_state-Signal darf das Ranking NUR verschieben, wenn die
# Item-Zelle mindestens so viele Samples hat. Darunter bleibt das Signal komplett
# stumm: kein Score-Schub UND kein Note-Text (keine "80% Win"-Texte auf 6 Spielen).
RANK_MIN_N = 30

# Konfidenz-Stufe je Champion+Rolle-Kombi (Review Befund 4, Gegenmassnahme 1):
# das Gate NICHT pro Zelle (das ist RANK_MIN_N), sondern pro KOMBI. Nur ueber
# CONF_RICH_MIN Spielen sind die konditionalen Schichten (by_threat/by_state)
# ueberhaupt belastbar - darunter werden sie gar nicht erst angewendet und die UI
# kommuniziert das ehrlich, statt still zu verrauschen. Der Wert 400 ist der
# Review-Richtwert; der Offline-Backtest (report-ablation.yaml, Patch 16.13)
# zeigt fuer diesen Pool KEIN positives Hit@3-Signal von by_threat/by_state bei
# irgendeinem n (neutral bis leicht negativ) - die Stufe ist damit primaer ein
# Ehrlichkeits-Mechanismus und bei Yorick (n=80) rein additiv (die Zellen liegen
# ohnehin unter RANK_MIN_N, das Gating aendert das Ranking dort nicht).
CONF_RICH_MIN = 400

# Schwelle fuer "CC-lastiges Gegnerteam" (Team-Mittel der Champion-CC-Priors,
# cc_per_min): darueber lohnen sich Tenacity-Boots (Mercury's Treads) vor der
# reinen Schadens-Split-Regel. Kalibriert auf die Team-CC-Score-Verteilung ueber
# alle gecachten 16.13-Matches (34540 Team-Scores aus 17270 Matches, Skript
# tmp/calibrate_cc_threshold.py): p70=0.983, p72=0.995, p75=1.015. Glatter Wert
# 1.0 liegt beim ~72.-73. Perzentil - die CC-lastigsten ~28% der Gegnerteams.
CC_HEAVY_THRESHOLD = 1.0


def _spike_warnings(enemy_profiles: list[dict], my_completed: int) -> list[dict]:
    """Awareness-Regel ohne jede Champion-Statistik (Review Befund 4.2): hat ein
    Gegner >= 1 fertiges Item mehr als der Spieler, eine Spike-Warnung erzeugen
    (>= 2 dringlicher). Reine Information - KEIN Eingriff ins Scoring/die Stance."""
    out = []
    for e in enemy_profiles:
        ec = e.get("completed_items")
        if ec is None:
            continue
        ahead = ec - my_completed
        if ahead < 1:
            continue
        plural = "Items" if ahead > 1 else "Item"
        out.append({
            "name": e.get("name"),
            "enemy_items": ec,
            "my_items": my_completed,
            "ahead_by": ahead,
            "urgency": "high" if ahead >= 2 else "medium",
            "message": f"{e.get('name')} ist dir {ahead} {plural} voraus",
        })
    out.sort(key=lambda w: -w["ahead_by"])
    # Deckel (Review Befund H.1): liegt man hinten, wuerden sonst alle fuenf
    # Gegner gleichzeitig warnen (Alarm-Tapete). Nur die 2 groessten Vorspruenge.
    return out[:2]


def confidence_tier(kb: dict) -> str:
    """'rich' | 'basic' | 'thin' aus der Datenlage der Kombi.
    thin  = gar kein KB-Eintrag (unter cfg.min_games kein Build-Wissen),
    basic = Eintrag vorhanden, aber unter CONF_RICH_MIN (situative Schichten
            zu duenn - nur der Core-Pfad ist belastbar),
    rich  = >= CONF_RICH_MIN Spiele, volles Verhalten."""
    if not kb:
        return "thin"
    return "rich" if kb.get("games", 0) >= CONF_RICH_MIN else "basic"


def _shrunk(win_rate: float, n: int, base: float, k: float = SHRINK_K) -> float:
    """Geschrumpfte Win-Rate Richtung Basisrate. Duenner Wrapper um
    core.stats.shrunk (Befund D3) mit SHRINK_K als Item-Prior-Default."""
    return stats.shrunk(win_rate, n, base, k)


# Reine Sustain-Stats, bei denen ein zweites fertiges Item kaum Mehrwert bringt
# (anders als Ruestung/MR/HP, die Tanks bewusst stapeln - die NICHT abwerten).
_REDUNDANT_TAGS = {"LifeSteal", "SpellVamp"}


def _redundant_stack(name: str, owned_names: set[str]) -> bool:
    """True, wenn der Kandidat einen Sustain-Stat traegt, den ein bereits
    besessenes FERTIGES Item schon liefert (zweiter Lifesteal lohnt selten)."""
    cand = items.tags_of(name) & _REDUNDANT_TAGS
    if not cand:
        return False
    for owned in owned_names:
        oe = items.by_name().get(owned)
        if oe and not oe[1].get("into") and cand & set(oe[1].get("tags", [])):
            return True
    return False


def _synergy_boost(name: str, owned_ids: list[int], factor: float = 0.3) -> float:
    """0..factor - je weiter der Spieler ueber vorhandene Komponenten schon in
    dieses Item investiert hat, desto hoeher wird es priorisiert (nicht nur
    billiger). Belohnt 'fertigbauen, was ich angefangen habe'."""
    if not owned_ids:
        return 0.0
    entry = items.by_name().get(name)
    if not entry:
        return 0.0
    total = entry[1].get("gold", {}).get("total", 0) or 1
    disc = items.build_discount(name, owned_ids)
    return factor * min(disc / total, 1.0)


@dataclass
class _RecContext:
    """Langlebige Zwischenergebnisse einer recommend()-Auswertung (Struktur-
    Review 2026-07-17 T3, Befund S1). Buendelt Kontext- und Kandidaten-Daten, die
    ueber mehrere Phasen (Core-Pick, Boots, konditionale Schichten, Scoring,
    Result-Assembly) hinweg leben, statt sie als lange Argumentketten
    durchzureichen. `_build_context` baut das Objekt auf; `_conditional_layers`
    befuellt die konditionalen/Klassen-Felder nach."""
    # Rohe Aufruf-Parameter, die spaetere Phasen noch brauchen
    champion: str
    used_role: str | None
    role: str | None
    cid: str
    owned_names: set
    owned_ids: list
    enemy_profiles: list
    ally_items: set
    game_time: float
    current_gold: int | None
    weights: Weights
    # Botlane-Partner-Kontext (research_bot_sup_mates.md 9.40): das Profil-Dict
    # des eigenen BOTTOM-Partners plus Schluessel `partner_class`, oder None fuer
    # alle Nicht-UTILITY-Faelle. In dieser Tranche nur abgelegt (Durchreichung);
    # der auswertende Layer folgt in T4.
    bot_partner: dict | None
    # Wissensbasis + abgeleitete Kontext-Kennzahlen (Phase 1)
    kb: dict
    top: dict | None
    split: dict
    enemy_cc_score: float
    fielded_lead: int | None
    earned_lead: int | None
    gold_state: str | None
    stance: str
    stance_reason: str
    build: dict
    build_reason: str
    core_source: list
    situational_source: list
    confidence: str
    has_boots: bool
    boots_options: list
    # Konditionale Schichten (by_threat/by_state) - von _conditional_layers gefuellt
    enemy_bucket: str | None = None
    bt: dict | None = None
    threat_items: dict = field(default_factory=dict)
    threat_base: float | None = None
    state_items: dict = field(default_factory=dict)
    state_base: float | None = None
    # Partner-konditioniert (by_partner, Phase 2) - von _conditional_layers gefuellt
    partner_items: dict = field(default_factory=dict)
    partner_base: float | None = None
    partner_bucket: str | None = None
    # Klassen-Fallback - von _conditional_layers gefuellt
    lookup_role: str = ""
    class_bucket: str | None = None
    class_situational: list = field(default_factory=list)
    class_boots: list = field(default_factory=list)
    class_games: int = 0
    class_label: str = ""
    champ_pool: set = field(default_factory=set)


def _build_context(champion: str, role: str | None, owned_names: set[str],
                   my_scores: dict, enemy_profiles: list[dict],
                   game_time: float, current_gold: int | None,
                   owned_ids: list[int] | None, my_level: int,
                   ally_items: set[str] | None, weights: Weights,
                   champion_id: str | None,
                   ally_gold_spent: int | None = None,
                   bot_partner: dict | None = None) -> _RecContext:
    """Phase 1 (Befund S1): KB-/Kontext-Aufbau - Rolle, Threat, Split, CC, Lead,
    Stance, Archetyp. Baut das _RecContext-Objekt fuer die folgenden Phasen."""
    # Stabile Data-Dragon-ID fuer alle internen Lookups (Fix 5.7): KB, Klassen-
    # Bucket. Der Anzeigename `champion` bleibt fuer die Begruendungstexte (UI).
    # champion_id kommt live aus profiling; fehlt es (Tests/Backtest, wo champion
    # bereits die ID ist), wird es aufgeloest.
    cid = champion_id or champions.resolve_id(champion) or champion
    used_role, kb = knowledge.for_champion(cid, role)
    top = max(enemy_profiles, key=lambda p: p["threat_score"], default=None)
    split = profiling.team_damage_split(enemy_profiles)
    # CC-Last des Gegnerteams (Team-Mittel der Champion-CC-Priors) - steuert die
    # Tenacity-Boots-Regel und wird fuer die UI im Result ausgewiesen.
    enemy_cc_score = profiling.team_cc_score(enemy_profiles)
    # Gemessenes Item-Gold des eigenen Spielers (identisch zur profiling-Metrik:
    # Summe gold.total des Inventars). Basis fuer beide Vorspruenge.
    my_gold_spent = items.categorize_gold(owned_ids or [])["gold_total"]
    # (A) Anzeige- und Stance-Vorsprung = gemessenes Item-Gold vs. Gegenpart.
    f_lead, opp = fielded_lead(my_gold_spent, used_role, enemy_profiles)
    # (B) getrennte Schaetzung des VERDIENTEN Golds fuer gold_state (KB-by_state).
    e_lead = earned_lead(my_gold_spent, current_gold, opp)
    # Team-Kontext fuer die Anzeige-Note: Item-Gold des eigenen Teams (ich +
    # Mitspieler) minus Gegnerteam. Ohne Mitspieler-Gold (Backtest) weggelassen.
    if ally_gold_spent is None:
        team_lead = None
    else:
        enemy_gold = sum(e.get("gold_spent", 0) for e in enemy_profiles)
        team_lead = int(my_gold_spent + ally_gold_spent - enemy_gold)
    note = lead_note(f_lead, opp, team_lead, current_gold)
    # Absolutes Fed-Signal (Review-Befund E): Stance kippt nur noch auf defensiv,
    # wenn ein Gegner GEMESSEN an der Spielzeit stark fed ist - nicht schon, weil
    # er relativ der reichste Gegner ist.
    enemy_fed = profiling.any_strongly_fed(enemy_profiles, game_time)
    stance, stance_reason = own_stance(my_scores, enemy_fed, f_lead, note)

    # Build-Archetyp anhand der bereits gekauften Items waehlen (Kernstueck A).
    # Ohne "builds" (Alt-Schema) faellt es auf die globalen core/situational
    # zurueck - so bleibt es abwaertskompatibel.
    if weights.use_archetypes:
        build, build_reason = _select_archetype(kb.get("builds", []), owned_names, stance)
    else:
        # Ablation: Archetyp-Auswahl abschalten -> Fallback auf globales
        # core/situational (Alt-Schema-Pfad).
        build, build_reason = {}, ""
    core_source = build.get("core") if build else kb.get("core", [])
    situational_source = build.get("situational") if build else kb.get("situational", [])

    # Gate: nur Items, die im aktuellen Data Dragon existieren UND auf SR gueltig
    # sind (maps["11"]). Filtert Legacy-/entfernte Items aus der KB (z.B. Galeforce).
    core_source = [e for e in core_source if items.is_valid_sr(e["item"])]
    situational_source = [e for e in situational_source if items.is_valid_sr(e["item"])]

    # Gold-konditioniert (Task 10): liege ich im VERDIENTEN Gold klar vorne/
    # hinten, zaehlt, was in genau dieser Lage gewinnt (bias-korrigierte `edge`
    # aus den Timelines). Schwelle = STATE_LEAD_GOLD, an die Train-Definition
    # (pipeline.aggregate.GOLD_LEAD) angeglichen - NICHT der fielded_lead.
    gold_state = ("ahead" if e_lead is not None and e_lead >= STATE_LEAD_GOLD
                  else "behind" if e_lead is not None and e_lead <= -STATE_LEAD_GOLD else None)

    boots_options = kb.get("boots", [])
    boots_options = [e for e in boots_options if items.is_valid_sr(e["item"])]
    # Nur "echte" Boots (T2+) zaehlen als vorhanden - die 300-G-Basis-Boots
    # duerfen das Upgrade nicht dauerhaft unterdruecken (s. items.is_upgraded_boots).
    has_boots = any(items.is_upgraded_boots(name) for name in owned_names)

    return _RecContext(
        champion=champion, used_role=used_role, role=role, cid=cid,
        owned_names=owned_names, owned_ids=owned_ids or [],
        enemy_profiles=enemy_profiles, ally_items=ally_items or set(),
        game_time=game_time, current_gold=current_gold, weights=weights,
        bot_partner=bot_partner,
        kb=kb, top=top, split=split, enemy_cc_score=enemy_cc_score,
        fielded_lead=f_lead, earned_lead=e_lead,
        gold_state=gold_state, stance=stance, stance_reason=stance_reason,
        build=build, build_reason=build_reason, core_source=core_source,
        situational_source=situational_source, confidence=confidence_tier(kb),
        has_boots=has_boots, boots_options=boots_options)


def _core_pick(ctx: _RecContext, recs: list[dict]) -> None:
    """Phase 2 (Befund S1): naechstes Core-Item, das noch fehlt (core ist nach
    Kaufreihenfolge sortiert). Haengt hoechstens ein Core-Item an recs."""
    for core in ctx.core_source:
        if core["item"] not in ctx.owned_names and not items.conflicts(core["item"], ctx.owned_names):
            reason = f"Core-Build fuer {ctx.champion} {ctx.used_role}"
            if ctx.build_reason:
                reason += f" - {ctx.build_reason}"
            purpose = explain_item(core["item"], ctx.split, ctx.top, role=_tag_role(ctx))
            if purpose:
                reason += f" - {purpose}"
            reason += f" ({core['pick_rate']:.0%} Pick-Rate in High-Elo"
            if core.get("avg_slot"):
                reason += f", typisch {core['avg_slot']:.0f}. Kauf"
            recs.append({"item": core["item"], "kind": "core",
                         "tag": _item_tag(core["item"], role=_tag_role(ctx)),
                         "reason": reason + ").",
                         "avg_slot": core.get("avg_slot")})
            break


def _choose_boots(options: list[dict], enemy_cc_score: float,
                  enemy_profiles: list[dict], *, popular_reason, cc_reason,
                  alt_reason, source: str | None = None,
                  situational=None, role: str | None = None) -> list[dict]:
    """Gemeinsame Boots-Wahl fuer KB- und Klassen-Pfad (Befund D5): CC-lastiges
    Gegnerteam (enemy_cc_score >= CC_HEAVY_THRESHOLD) -> Tenacity-Boots mit
    cc_threats-Begruendung, sonst meistgespielte; weicht die Wahl von den
    meistgespielten ab -> letztere als alternative-Rec anhaengen. Die
    unterschiedlichen Reason-Texte kommen als Callbacks:
      popular_reason(chosen)   -> Begruendung der meistgespielten Boots
      cc_reason(mercs, who)    -> Begruendung bei viel Gegner-CC (`who` = die
                                  bereits formatierte cc_threats-Liste)
      alt_reason(popular)      -> Begruendung der alternative-Rec
    Die situative AD/AP-Boots-Wahl (`situational(popular) -> (chosen, reason) |
    None`) gibt es NUR im KB-Pfad. Gibt die anzuhaengenden recs zurueck."""
    out: list[dict] = []
    popular = options[0]
    chosen = popular
    reason = popular_reason(chosen)
    # VORRANG-Regel: viel CC im Gegnerteam -> Tenacity-Boots (Mercury's Treads),
    # auch wenn sie nicht die meistgespielten sind. CC ist der eigentliche
    # Kaufgrund fuer Mercs, nicht der AP-Schaden.
    mercs = next((b for b in options if items.is_tenacity_boots(b["item"])), None)
    if enemy_cc_score >= CC_HEAVY_THRESHOLD and mercs:
        chosen = mercs
        cc_names = profiling.cc_threats(enemy_profiles)
        who = f" ({', '.join(cc_names)})" if cc_names else ""
        reason = cc_reason(mercs, who)
    elif situational is not None:
        alt = situational(popular)
        if alt is not None:
            chosen, reason = alt
    # Primaere (situative) Boots zuerst - _pick_next nimmt genau diese als
    # Kandidat. Weicht sie von den meistgespielten ab, die meistgespielten als
    # ZWEITE Option (reine Sichtbarkeit, keine Reihenfolge-Wirkung).
    rec = {"item": chosen["item"], "kind": "boots", "reason": reason,
           "tag": _item_tag(chosen["item"], role=role), "avg_slot": chosen.get("avg_slot")}
    if source:
        rec["source"] = source
    out.append(rec)
    if chosen["item"] != popular["item"]:
        alt_rec = {"item": popular["item"], "kind": "boots",
                   "reason": alt_reason(popular), "tag": _item_tag(popular["item"], role=role),
                   "avg_slot": popular.get("avg_slot"), "alternative": True}
        if source:
            alt_rec["source"] = source
        out.append(alt_rec)
    return out


def _boots_kb(ctx: _RecContext) -> list[dict]:
    """Phase 3, KB-Pfad (Befund S1/D5): Boots aus der Champion-KB nach
    Gegnerschaden waehlen, wenn welche gelernt sind und der Spieler noch keine
    traegt. Die situative AD/AP-Wahl gibt es NUR hier."""
    if not ctx.boots_options or ctx.has_boots:
        return []
    split, top, options = ctx.split, ctx.top, ctx.boots_options

    def situational(popular):
        # Gewuenschte Defensiv-Richtung bestimmen (None = keine situative Wahl)
        want = None
        want_reason = ""
        if split["ad"] >= 0.65:
            want = "ad"
            want_reason = f"Gegnerschaden ist {split['ad']:.0%} AD - Ruestungs-Boots."
        elif split["ap"] >= 0.55:
            want = "ap"
            want_reason = f"Gegnerschaden ist {split['ap']:.0%} AP - MR-Boots."
        elif top is not None:
            # Tie-Breaker: ausgeglichener Team-Split, aber staerkste Bedrohung
            # hat klar einseitigen damage_split -> gegen diesen Typ ausrichten.
            top_split = top.get("damage_split")
            if top_split:
                if top_split.get("ad", 0) >= 0.6:
                    want = "ad"
                    want_reason = (f"Staerkste Bedrohung {top['name']} ist "
                                   f"klar AD-lastig - Ruestungs-Boots.")
                elif top_split.get("ap", 0) >= 0.6:
                    want = "ap"
                    want_reason = (f"Staerkste Bedrohung {top['name']} ist "
                                   f"klar AP-lastig - MR-Boots.")
        if want:
            # Kandidat aus der KB-Boots-Liste oder kanonischer Fallback
            kb_boot = next((b for b in options if _is_defensive(b["item"], want)), None)
            if kb_boot:
                return kb_boot, want_reason
            fb = items.standard_defensive_boots(want)
            if fb:
                return {"item": fb, "avg_slot": None}, want_reason
        return None

    def alt_reason(popular):
        text = f"Alternative: meistgespielte Boots ({popular['pick_rate']:.0%}"
        if popular.get("avg_slot"):
            text += f", typisch {popular['avg_slot']:.0f}. Kauf"
        return text + ")."

    return _choose_boots(
        options, ctx.enemy_cc_score, ctx.enemy_profiles,
        popular_reason=lambda c: f"Meistgespielte Boots ({c['pick_rate']:.0%}).",
        cc_reason=lambda m, who: f"Viel CC im Gegnerteam{who} - {m['item']} (Tenacity).",
        alt_reason=alt_reason, situational=situational, role=_tag_role(ctx))


def _boots_class(ctx: _RecContext) -> list[dict]:
    """Phase 3, Klassen-Pfad (Befund S1/D5): hat der Champion selbst keine
    Boots-Daten (thin) und traegt der Spieler noch keine, die meistgespielten
    Boots des Buckets empfehlen - klar gelabelt. Auch hier hat viel Gegner-CC
    Vorrang (Tenacity vor den meistgespielten Klassen-Boots)."""
    if not ctx.class_boots or ctx.boots_options or ctx.has_boots:
        return []
    label, lrole = ctx.class_label, ctx.lookup_role
    return _choose_boots(
        ctx.class_boots, ctx.enemy_cc_score, ctx.enemy_profiles,
        popular_reason=lambda c: (f"Meistgespielte Boots der Klasse "
                                  f"({label} {lrole}, {c['pick_rate']:.0%})."),
        cc_reason=lambda m, who: (f"Viel CC im Gegnerteam{who} - {m['item']} "
                                  f"(Tenacity, aus Klassen-Daten {label} {lrole})."),
        alt_reason=lambda popular: (f"Alternative: meistgespielte Boots der Klasse "
                                    f"({label} {lrole}, {popular['pick_rate']:.0%})."),
        source="class", role=_tag_role(ctx))


def _conditional_layers(ctx: _RecContext) -> None:
    """Phase 4 (Befund S1): by_threat-/by_state-Schichten vorbereiten inkl.
    Konfidenz-Gate und Klassen-Fallback-Datenbeschaffung. Fuellt die
    konditionalen und Klassen-Felder des ctx."""
    kb = ctx.kb
    # Threat-konditionierte Item-Win-Rates (Schicht 4, datengetrieben): gegen
    # ein klar AD- bzw. AP-lastiges Gegnerteam zaehlt, was empirisch gewonnen hat.
    # WICHTIG (Review G): Der by_threat-Lookup nutzt die TRAIN-Definition -
    # ungewichtetes Mittel der Champion-Priors (champions.damage_bucket), NICHT
    # das threat-gewichtete `split`. Nur so werden die gelernten Zellen unter
    # derselben Definition abgefragt, unter der sie gezaehlt wurden. Gegner ohne
    # bekannten Prior werden wie auf der Train-Seite weggelassen; `split` bleibt
    # fuer Boots-/Defensiv-Texte unveraendert threat-gewichtet.
    _enemy_shares = [s for s in (champions.ad_share_for_id(p.get("champion_id"))
                                 for p in ctx.enemy_profiles) if s is not None]
    enemy_bucket = champions.damage_bucket(_enemy_shares)
    if enemy_bucket not in ("ad", "ap"):
        enemy_bucket = None
    bt = kb.get("by_threat", {}).get(enemy_bucket) if enemy_bucket else None
    # Volle Item-Dicts behalten (count/win_rate) + Basisrate des Buckets fuer
    # Shrinkage und Ranking-Gate. base_win_rate kann bei kuratierten Overrides
    # oder alter KB fehlen -> dann Kuratiert-Pfad (kein Gate, keine Shrinkage).
    ctx.enemy_bucket = enemy_bucket
    ctx.bt = bt
    ctx.threat_items = {t["item"]: t for t in bt["items"]} if bt else {}
    ctx.threat_base = bt.get("base_win_rate") if bt else None

    # Gold-konditioniert (Task 10): liege ich klar vorne/hinten, zaehlt, was in
    # genau dieser Lage gewinnt (bias-korrigierte `edge` aus den Timelines).
    bs = kb.get("by_state", {}).get(ctx.gold_state) if ctx.gold_state else None
    ctx.state_items = {t["item"]: t for t in bs["items"]} if bs else {}
    ctx.state_base = bs.get("base_win_rate") if bs else None

    # Partner-konditioniert (by_partner, Phase 2): NUR fuer UTILITY. Partner-
    # Klasse -> "ad"/"ap" via damage_bucket auf den ad_share des Bot-Partners
    # (KONSISTENT zur Trainseite). Bucket-Lookup, Shrinkage und RANK_MIN_N-Gate
    # spiegeln exakt den by_threat-Mechanismus.
    partner_bucket = None
    if ctx.role == "UTILITY" and ctx.bot_partner:
        pcid = ctx.bot_partner.get("champion_id")
        if pcid:
            pshare = champions.ad_share_for_id(pcid)
            if pshare is not None:
                pb = champions.damage_bucket([pshare])
                if pb in ("ad", "ap"):
                    partner_bucket = pb
    bp_data = kb.get("by_partner", {}).get(partner_bucket) if partner_bucket else None
    ctx.partner_bucket = partner_bucket
    ctx.partner_items = {t["item"]: t for t in bp_data["items"]} if bp_data else {}
    ctx.partner_base = bp_data.get("base_win_rate") if bp_data else None

    # Konfidenz-Gate pro KOMBI: unterhalb von CONF_RICH_MIN sind die
    # konditionalen Schichten (by_threat/by_state/by_partner) zu duenn - gar
    # nicht erst anwenden (kein Score-Schub, kein "Win gegen"-Text), statt still
    # zu verrauschen. Der Core-Pfad, Stance und Archetyp-Wahl bleiben unberuehrt.
    if ctx.confidence != "rich":
        ctx.threat_items, ctx.state_items = {}, {}
        ctx.threat_base = ctx.state_base = None
        ctx.partner_items = {}
        ctx.partner_base = None

    # Klassen-Fallback (Review Befund 4.3): bei nicht-`rich` Kombis die situativen
    # Kandidaten um Items aus dem Klassen-Aggregat ERGAENZEN (z.B. AD-Fighter
    # JUNGLE statt 80 Yorick-Spielen). Die eigenen Champion-Kandidaten bleiben
    # unveraendert und ranken IMMER vor den Klassen-Kandidaten (rein additiv),
    # damit Champion-Evidenz Vorrang behaelt. Bei `rich` bleibt alles wie bisher.
    ctx.lookup_role = ctx.used_role or ctx.role or ""
    if ctx.confidence != "rich":
        ctx.class_bucket = champions.bucket_for_id(ctx.cid)
        if ctx.class_bucket:
            class_entry = knowledge.for_class(ctx.class_bucket, ctx.lookup_role)
            if class_entry:
                ctx.class_situational = [e for e in class_entry.get("situational", [])
                                         if items.is_valid_sr(e["item"])]
                ctx.class_boots = [e for e in class_entry.get("boots", [])
                                   if items.is_valid_sr(e["item"])]
                ctx.class_games = class_entry.get("games", 0)
    ctx.class_label = _bucket_label(ctx.class_bucket)
    # Namen, die der Champion-Pool bereits kennt -> Klassen-Duplikate auslassen.
    ctx.champ_pool = ({c["item"] for c in kb.get("core", [])}
                      | {s["item"] for s in kb.get("situational", [])}
                      | {b["item"] for b in kb.get("boots", [])})


def _partner_layer_active(ctx: _RecContext) -> bool:
    """Botlane-Partner-Layer (T4) feuert NUR, wenn der Spieler selbst UTILITY ist
    UND ein Bot-Partner mit belastbarer Klasse (ad_carry/ap_carry) vorliegt. Fuer
    jede andere Rolle oder eine unbekannte Partner-Klasse ein striktes No-Op."""
    if ctx.role != "UTILITY":
        return False
    bp = ctx.bot_partner
    return bool(bp) and bp.get("partner_class") in ("ad_carry", "ap_carry")


def _tag_role(ctx: _RecContext) -> str | None:
    """Rolle fuer das Support-Framing der Tags/Erklaertexte (T4b): nur "UTILITY"
    aktiviert die Support-Kategorien, sonst None (byte-identisches Verhalten)."""
    return ctx.role if ctx.role == "UTILITY" else None


def _partner_adjust(ctx: _RecContext, name: str, score: float,
                    why: str) -> tuple[float, str]:
    """Botlane-Partner-Layer (T4, nur bei aktivem _partner_layer_active):

    - Ally-Buff-Matching (hart): das zum Partner-Schadenstyp passende Ally-Buff-
      Item (Staff@ap_carry / Ardent@ad_carry) bekommt +partner_buff_boost, das
      falsche -partner_buff_penalty (Demotion, kein stilles Entfernen).
    - Archetyp-Tilt (weich): bei ap_carry-Partner die uebrigen Heal/Shield-
      Enchanter um partner_enchanter_tilt runter. Matching hat Vorrang - bereits
      gematchte Ally-Buff-Items (Staff!) fasst der Tilt NICHT an.
    Jede Anpassung an einem empfohlenen Item bekommt einen Begruendungszusatz."""
    bp = ctx.bot_partner
    pclass = bp.get("partner_class")
    pname = bp.get("name") or "Bot-Partner"
    w = ctx.weights
    note = ""
    matched = name in items.ALLY_ONHIT_ITEMS | items.ALLY_AP_ITEMS
    if pclass == "ap_carry":
        if name in items.ALLY_AP_ITEMS:
            score += w.partner_buff_boost
            note = (f" - dein Bot-Partner ({pname}) ist ein AP-Carry - "
                    f"{name} verstaerkt ihn direkt")
        elif name in items.ALLY_ONHIT_ITEMS:
            score -= w.partner_buff_penalty
            note = (f" - dein Bot-Partner ({pname}) ist ein AP-Carry - "
                    f"{name} bringt deinem AP-Partner nichts")
    elif pclass == "ad_carry":
        if name in items.ALLY_ONHIT_ITEMS:
            score += w.partner_buff_boost
            note = (f" - dein Bot-Partner ({pname}) ist ein AD-Carry - "
                    f"{name} verstaerkt ihn direkt")
        elif name in items.ALLY_AP_ITEMS:
            score -= w.partner_buff_penalty
            note = (f" - dein Bot-Partner ({pname}) ist ein AD-Carry - "
                    f"{name} bringt deinem AD-Partner nichts")
    # Weicher Archetyp-Tilt: bei AP-Partner baut die Botlane seltener klassische
    # Heal/Shield-Enchanter (Befund 2). Nur auf die NICHT gematchten Enchanter.
    if pclass == "ap_carry" and not matched and name in items.ENCHANTER_ITEMS:
        score -= w.partner_enchanter_tilt
        note = (f" - mit einem AP-Carry ({pname}) am Bot dreht der Support "
                f"seltener auf Heal/Shield-Enchanter")
    if note:
        why = why.rstrip(".") + note + "."
    return score, why


def _score_situationals(ctx: _RecContext, recs: list[dict]) -> None:
    """Phase 6 (Befund S1): der grosse Scoring-Loop ueber situational_source +
    Klassen-Kandidaten inkl. Zwei-Pass-defensive_stance-Boost, Sortierung und
    Top-3-Auswahl. Haengt die gewaehlten situativen Items an recs."""
    weights = ctx.weights
    split, top, stance = ctx.split, ctx.top, ctx.stance
    threat_items, threat_base = ctx.threat_items, ctx.threat_base
    enemy_bucket, bt = ctx.enemy_bucket, ctx.bt
    state_items, state_base = ctx.state_items, ctx.state_base
    gold_state, owned_names, owned_ids = ctx.gold_state, ctx.owned_names, ctx.owned_ids
    class_label, lookup_role, class_games = ctx.class_label, ctx.lookup_role, ctx.class_games
    partner_on = _partner_layer_active(ctx)
    tag_role = _tag_role(ctx)

    # 3. Situative Items, nach Stance und Gegnerteam umsortiert. Zuerst die
    # eigenen Champion-Kandidaten (`scored`), dann - nur bei aktivem Fallback -
    # die zusaetzlichen Klassen-Kandidaten (`class_scored`).
    scored = []
    class_scored = []
    class_seen = set()
    sources = [(e, "champion") for e in ctx.situational_source]
    for entry in ctx.class_situational:
        name = entry["item"]
        if name in ctx.champ_pool or name in class_seen:
            continue
        class_seen.add(name)
        sources.append((entry, "class"))
    for entry, source in sources:
        name = entry["item"]
        if name in owned_names or items.conflicts(name, owned_names):
            continue
        score = entry["pick_rate"]
        # Win-Rates nie ohne n: wo die KB die Fallzahl mitliefert, ausweisen.
        n_txt = f", n={entry['count']}" if entry.get("count") is not None else ""
        stats = (f"{entry['pick_rate']:.0%} Pick, {entry['win_rate']:.0%} Win"
                 f" in High-Elo{n_txt}")
        purpose = explain_item(name, split, top, role=tag_role)
        why = f"{purpose} ({stats})." if purpose else f"Haeufig in dieser Rolle gebaut ({stats})."
        # Empirische Schuebe (Ranking) + Notizen (Text) - Notizen werden erst
        # NACH dem Defensiv-Zweig angehaengt, damit dessen why-Neuaufbau sie
        # nicht verschluckt.
        extra = ""
        if name in threat_items:
            t = threat_items[name]
            n = t.get("count")
            if n is None or threat_base is None:
                # KURATIERT (Override/alte KB ohne count/base): Signal gilt als
                # vertrauenswuerdig - kein Gate, keine Shrinkage, Verhalten wie
                # bisher (Roh-Win-Rate gegen 0.5).
                score += max(-weights.threat_cap, min(
                    weights.threat_cap,
                    weights.threat_scale * (t["win_rate"] - 0.5)))
                extra += (f" - {t['win_rate']:.0%} Win gegen "
                          f"{enemy_bucket.upper()}-lastige Teams ({bt['games']} Spiele)")
            elif n >= RANK_MIN_N:
                # Geschrumpfte Rate gegen die Bucket-Basisrate: nur echte,
                # ausreichend belegte Abweichungen verschieben das Ranking.
                wr = _shrunk(t["win_rate"], n, threat_base)
                score += max(-weights.threat_cap, min(
                    weights.threat_cap, weights.threat_scale * (wr - threat_base)))
                extra += (f" - {t['win_rate']:.0%} Win gegen "
                          f"{enemy_bucket.upper()}-lastige Teams (n={n})")
            # n < RANK_MIN_N: Signal komplett stumm (kein Schub, kein Text).
        if name in state_items:
            t = state_items[name]
            n = t.get("count")
            lage = "vorne" if gold_state == "ahead" else "hinten"
            if n is None or state_base is None:
                # KURATIERT: rohe edge wie bisher, kein Gate.
                score += max(-weights.state_cap,
                             min(weights.state_cap, t.get("edge", 0.0)))
                extra += f" - ueberdurchschnittlich, wenn du {lage} liegst"
            elif n >= RANK_MIN_N:
                # edge neu aus geschrumpfter Item-Win-Rate minus Basisrate.
                wr = _shrunk(t["win_rate"], n, state_base)
                score += max(-weights.state_cap, min(weights.state_cap, wr - state_base))
                extra += f" - {t['win_rate']:.0%} Win, wenn du {lage} liegst (n={n})"
            # n < RANK_MIN_N: Signal stumm.
        # Partner-konditioniert (by_partner, Phase 2): NUR bei UTILITY + rich +
        # gueltiger Partner-Bucket. Exakt wie by_threat: Shrinkage + RANK_MIN_N-
        # Zellgate + Cap/Scale.
        if name in ctx.partner_items:
            t = ctx.partner_items[name]
            n = t.get("count")
            plabel = ctx.partner_bucket.upper() if ctx.partner_bucket else "?"
            if n is None or ctx.partner_base is None:
                # Kuratiert: kein Gate, keine Shrinkage.
                score += max(-weights.partner_kb_cap, min(
                    weights.partner_kb_cap,
                    weights.partner_kb_scale * (t["win_rate"] - 0.5)))
                extra += (f" - {t['win_rate']:.0%} Win mit "
                          f"{plabel}-Partner")
            elif n >= RANK_MIN_N:
                wr = _shrunk(t["win_rate"], n, ctx.partner_base)
                score += max(-weights.partner_kb_cap, min(
                    weights.partner_kb_cap,
                    weights.partner_kb_scale * (wr - ctx.partner_base)))
                extra += (f" - {t['win_rate']:.0%} Win mit "
                          f"{plabel}-Partner (n={n})")
            # n < RANK_MIN_N: Signal stumm.
        vs = "ad" if split["ad"] >= split["ap"] else "ap"
        defensive = _is_defensive(name, vs)
        # Der defensive_stance-Boost bei defensiver Stance wird NICHT hier pro
        # Item vergeben (das machte frueher alle drei Situationals defensiv,
        # Review 5.3), sondern erst NACH dem Scoring auf genau das bestplatzierte
        # defensive Item (Zwei-Pass). Die uebrigen Stance-Boosts bleiben pro Item.
        if defensive:
            if stance == "defensive":
                threat = f" - Top-Threat: {top['name']} ({top['build_profile']})" if top else ""
                why = f"{purpose}{threat} ({stats})." if purpose else why
            elif stance == "balanced":
                score += weights.defensive_balanced
        elif stance == "aggressive":
            score += weights.aggressive_offense
        if extra:
            why = why.rstrip(".") + extra + "."
        # B: Synergie - schon investierte Komponenten ziehen das Item hoch;
        # Redundanz - zweites reines Sustain-Item wird abgewertet.
        score += _synergy_boost(name, owned_ids, weights.synergy_factor)
        if _redundant_stack(name, owned_names):
            score -= weights.redundancy_penalty
        # Botlane-Partner-Layer (T4): Ally-Buff-Matching + Enchanter-Tilt. Wirkt
        # auf Champion- UND Klassen-Kandidaten (der class-Reason-Zusatz wird erst
        # danach angehaengt, der Partner-Hinweis bleibt also erhalten).
        if partner_on:
            score, why = _partner_adjust(ctx, name, score, why)
        rec = {"item": name, "kind": "situational", "tag": _item_tag(name, role=tag_role),
               "reason": why, "defensive": defensive,
               "avg_slot": entry.get("avg_slot")}
        if source == "class":
            # Klar als Klassen-Fallback labeln (source + Reason-Zusatz), damit der
            # Nutzer Champion-Evidenz von Klassen-Daten unterscheiden kann.
            rec["source"] = "class"
            rec["reason"] = (why.rstrip(".") +
                             f" - aus Klassen-Daten ({class_label} {lookup_role}, "
                             f"n={class_games}).")
            class_scored.append([score, rec])
        else:
            scored.append([score, rec])
    # Variante 1 (Review 5.3, Backtest-belegt): bei defensiver Stance bekommt NUR
    # das bestplatzierte defensive Item den defensive_stance-Boost - eine
    # 1/4-Gwen braucht ein defensives Zugestaendnis, nicht drei Tank-Optionen.
    # Bei balanced/aggressive bleibt alles unveraendert.
    if stance == "defensive" and weights.defensive_stance:
        defensive_rows = [row for row in scored if row[1]["defensive"]]
        if defensive_rows:
            best_def = max(defensive_rows, key=lambda row: row[0])
            best_def[0] += weights.defensive_stance
    scored.sort(key=lambda row: row[0], reverse=True)
    class_scored.sort(key=lambda row: row[0], reverse=True)
    # Klassen-Kandidaten IMMER hinter den eigenen ranken (additiv): so kann der
    # Fallback nur leere Plaetze fuellen, aber keinen Champion-Kandidaten aus den
    # Top-3 verdraengen (Hit@3 kann dadurch nicht sinken).
    recs.extend(rec for _, rec in (scored + class_scored)[:3])


def _assemble_result(ctx: _RecContext, recs: list[dict],
                     antiheal: dict | None) -> dict:
    """Phase 7 (Befund S1): naechster Kauf, Halbfertig-Erkennung und Result-Dict
    inkl. Confidence-Notes."""
    kb = ctx.kb
    kb_names = ({c["item"] for c in kb.get("core", [])}
                | {s["item"] for s in kb.get("situational", [])}
                | {b["item"] for b in kb.get("boots", [])})
    next_pick = _pick_next(recs, ctx.stance, ctx.owned_names, ctx.game_time,
                           ctx.current_gold, ctx.owned_ids,
                           stance_next=ctx.weights.stance_next,
                           role=_tag_role(ctx),
                           slot_role=ctx.role or ctx.used_role)
    # Halbfertiges Item aus den Komponenten erkennen (rein informativ - die
    # Empfehlung bleibt unveraendert). Ausblenden, wenn es OHNEHIN das naechste
    # empfohlene Item ist: dann zeigt der "Naechstes Item"-Block die Restkosten
    # schon, und die Zeile waere nur doppelt.
    building = items.in_progress(ctx.owned_ids, kb_names)
    if building and next_pick and building["item"] == next_pick.get("item"):
        building = None
    result = {
        "role": ctx.used_role,
        "stance": ctx.stance,
        "stance_reason": ctx.stance_reason,
        # Klarstellung, dass die Item-Empfehlung trotz defensiver Stance der
        # gelernten Kaufreihenfolge folgt (Befund H, review-2026-07-15.md /
        # Befund D, 2026-07-13). Leer, wenn nicht defensiv oder Stance aktiv.
        "stance_note": _stance_note(ctx.stance, ctx.weights),
        "enemy_damage_split": ctx.split,
        "enemy_cc_score": ctx.enemy_cc_score,
        "knowledge_games": kb.get("games", 0),
        "confidence": ctx.confidence,
        "build": ctx.build.get("name") if ctx.build else None,
        "build_reason": ctx.build_reason,
        "builds_available": [b["name"] for b in kb.get("builds", [])],
        "antiheal": antiheal,
        "building": building,
        "next": next_pick,
        # Kaufplan-Leiste (Feature 001): flache Schritt-Liste unter dem Next-Item.
        # Das zweite grosse Item entsteht aus einer erneuten Empfehlung mit dem
        # Next-Item als 'besessen' (loest u.a. Passive-Kollisionen sauber auf).
        "purchase_plan": _purchase_plan(
            ctx, recs, next_pick,
            second_pick=(_second_next_pick(ctx, next_pick)
                         if next_pick and next_pick.get("kind") != "consumable"
                         else None)),
        "items": recs,
        # Awareness (Review Befund 4.2): Gegner, die dem Spieler an fertigen
        # Items voraus sind - reine Information, kein Scoring-Eingriff.
        "spike_warnings": _spike_warnings(
            ctx.enemy_profiles, items.count_completed(ctx.owned_ids)),
    }
    # Aktiver Klassen-Fallback? (mind. ein Klassen-Item in den Empfehlungen)
    class_used = any(r.get("source") == "class" for r in recs)
    if ctx.confidence == "basic":
        # Ehrlicher Hinweis: der Core-Pfad traegt, die situativen Signale nicht.
        note = (f"Basis-Empfehlung (n={kb.get('games', 0)}) - situative Signale "
                f"(Gegnerschaden/Spielstand) zu duenn, nur der Core-Pfad ist belastbar.")
        if class_used:
            note += (f" - ergaenzt um {ctx.class_label}-Daten (n={ctx.class_games}).")
        result["confidence_note"] = note
    if not kb:
        shown_role = ctx.role or ctx.used_role or "?"
        if class_used:
            # Reine Klassen-Empfehlung statt "kein Build-Wissen"-Ende: Situational
            # + Boots aus dem Bucket, klar gelabelt. next_pick bleibt erhalten.
            result["confidence_note"] = (
                f"Keine Champion-Daten fuer {ctx.champion} {shown_role} - Empfehlung "
                f"rein aus Klassen-Daten ({ctx.class_label} {ctx.lookup_role}, "
                f"n={ctx.class_games}).")
        else:
            # Weder Champion- noch Klassen-Daten -> NICHT als "Build komplett"
            # ausgeben. Ehrlicher Hinweis statt Elixier-Unsinn.
            result["next"] = None
            result["purchase_plan"] = None
            result["note"] = (f"Keine Build-Daten fuer {ctx.champion} {shown_role} in der "
                              f"Wissensbasis (Patch {knowledge.load()['patch']}). "
                              f"Rolle pruefen oder per focus-Crawl anreichern.")
    return result


def _support_final(ctx: _RecContext) -> list[dict]:
    """Support-Item-Endwahl (World-Atlas-Questlinie, Datenbefund 9.4x): sobald der
    Support die Quest fertig hat, waehlt er GENAU EINE der fuenf Endformen
    (COMPLETED_SUPPORT_ITEMS). Diese Wahl ist fast deterministisch vom eigenen
    Support-Champion bestimmt (nicht von Gegner-Comp oder Bot-Partner) - die
    Empfehlung ist darum CHAMPION-FEST.

    Feuert NUR, wenn (1) die eigene Rolle UTILITY ist, (2) der Spieler noch ein
    offenes Questketten-Item traegt (World Atlas/Runic Compass/Bounty of Worlds)
    UND (3) noch KEINE der fuenf Endformen besitzt. Ist die Wahl bereits getroffen
    (eine Endform im Inventar), liefert der Layer NICHTS mehr.

    Primaervorschlag = das fuer diesen Champion (UTILITY) haeufigste der fuenf
    Finals aus der Wissensbasis, wobei die Pick-Rate NUR relativ ueber die fuenf
    Finals gebildet wird. Optionaler defensiver Zweitvorschlag = Celestial
    Opposition (Schild), wenn die Lage defensiv ist (stance/gold_state) und
    Celestial nicht ohnehin der Primaervorschlag ist. Ohne KB-Daten fuer die
    Finals liefert der Layer konservativ NICHTS (kein erzwungener Primaervorschlag).
    """
    if ctx.role != "UTILITY" and ctx.used_role != "UTILITY":
        return []
    # (2) offenes Questketten-Item im Inventar?
    if not (set(ctx.owned_ids) & items.SUPPORT_QUEST_IDS):
        return []
    # (3) bereits eine Endform gewaehlt? -> Wahl getroffen, kein Vorschlag noetig.
    if ctx.owned_names & items.COMPLETED_SUPPORT_ITEMS:
        return []
    # Pick-Rates der fuenf Finals aus der Champion-KB (Roh-KB, nicht archetyp-
    # gefiltert - die Finals sind keiner Build-Variante zugeordnet). Pro Item die
    # hoechste gefundene Pick-Rate ueber core/situational nehmen.
    kb = ctx.kb
    rates: dict[str, float] = {}
    for section in (kb.get("core", []), kb.get("situational", [])):
        for entry in section:
            name = entry["item"]
            if name in items.COMPLETED_SUPPORT_ITEMS:
                rates[name] = max(rates.get(name, 0.0), entry.get("pick_rate", 0.0))
    if not rates:
        # Keine/zu duenne KB-Daten fuer die Finals -> konservativ nichts liefern
        # (analog Core-Pick: fehlt das Wissen, wird nichts erzwungen).
        return []
    total = sum(rates.values()) or 1.0
    primary = max(rates, key=rates.get)
    share = rates[primary] / total   # relativer Anteil NUR unter den Finals
    tag_role = _tag_role(ctx)
    reason = (f"Support-Item-Endwahl fuer {ctx.champion} {ctx.used_role}: {primary} "
              f"ist die champion-feste Wahl ({share:.0%} unter den Support-Endformen "
              f"in High-Elo).")
    out = [{"item": primary, "kind": "core",
            "tag": _item_tag(primary, role=tag_role),
            "reason": reason, "avg_slot": None}]
    # Defensiver Zweitvorschlag: Celestial Opposition (Schild), wenn die Lage
    # defensiv ist und es nicht ohnehin schon der Primaervorschlag ist.
    celestial = items.CELESTIAL_OPPOSITION
    if primary != celestial and (ctx.stance == "defensive" or ctx.gold_state == "behind"):
        out.append({"item": celestial, "kind": "situational",
                    "tag": _item_tag(celestial, role=tag_role),
                    "reason": ("Defensive Alternative: Schild-Item "
                               "bei Rueckstand/Unter-Druck."),
                    "defensive": True, "avg_slot": None})
    return out


def recommend(champion: str, role: str | None, owned_names: set[str],
              my_scores: dict, enemy_profiles: list[dict],
              game_time: float = 0.0, current_gold: int | None = None,
              owned_ids: list[int] | None = None, my_level: int = 0,
              ally_items: set[str] | None = None,
              weights: Weights = DEFAULT_WEIGHTS,
              champion_id: str | None = None,
              ally_gold_spent: int | None = None,
              bot_partner: dict | None = None) -> dict:
    """Orchestrator (Struktur-Review 2026-07-17 T3, Befund S1): baut den Kontext
    auf und ruft die Phasen-Helfer in fester Reihenfolge - Core-Pick, Boots
    (KB- und Klassen-Pfad), konditionale Schichten, situatives Scoring,
    Anti-Heal, Result-Assembly. Die eigentliche Logik liegt in den _*-Helfern."""
    ctx = _build_context(champion, role, owned_names, my_scores, enemy_profiles,
                         game_time, current_gold, owned_ids, my_level, ally_items,
                         weights, champion_id, ally_gold_spent, bot_partner)
    recs: list[dict] = []
    # 1. Naechstes Core-Item
    _core_pick(ctx, recs)
    # 2. Boots (KB-Pfad) nach Gegnerschaden
    recs += _boots_kb(ctx)
    # Konditionale Schichten (by_threat/by_state) + Klassen-Fallback-Daten
    _conditional_layers(ctx)
    # Klassen-Boots-Fallback (nur wenn der Champion selbst keine Boots-Daten hat)
    recs += _boots_class(ctx)
    # 3. Situative Items, nach Stance und Gegnerteam umsortiert
    _score_situationals(ctx, recs)

    # Schicht 4: Anti-Heal-Team-Coverage. Als situatives Item sichtbar machen
    # (Duplikate entfernen), aber NICHT als naechsten Pflichtkauf erzwingen -
    # der Core-Build hat Vorrang; Anti-Heal ist eine Option, kein Zwang.
    antiheal = _antiheal_recommendation(
        ctx.enemy_profiles, ctx.owned_names, ctx.ally_items, ctx.situational_source,
        _my_damage_type(ctx.owned_ids, ctx.core_source), ctx.game_time,
        struggling=(ctx.stance == "defensive" or ctx.gold_state == "behind"))
    if antiheal:
        recs = [r for r in recs if r["item"] != antiheal["item"]]
        recs.append(antiheal)

    # Schicht 5: Support-Item-Endwahl (champion-fest). Ist sie aktiv, ist sie die
    # EINZIGE Quelle fuer die fuenf Support-Endformen - eventuelle Finals aus dem
    # Core-/Situational-Pfad werden entfernt (Dedupe), der Primaervorschlag wird
    # vorangestellt, damit _pick_next ihn als naechsten Kauf nimmt.
    support = _support_final(ctx)
    if support:
        recs = [r for r in recs
                if r["item"] not in items.COMPLETED_SUPPORT_ITEMS]
        recs = support + recs

    return _assemble_result(ctx, recs, antiheal)


ITEM_SLOTS = 6  # regulaere Slots; Boots und Trinket haben eigene Slots


def _elixir_next(owned_ids: list[int], current_gold: int | None,
                 slot_role: str | None = None) -> dict | None:
    """Build komplett: Elixier passend zum eigenen Schadensprofil empfehlen -
    aber nur, wenn ein Slot frei ist (Consumables brauchen einen regulaeren
    Slot). Sonst gibt es nichts Sinnvolles zu kaufen. `slot_role` steuert die
    rollenbewusste Slot-Zaehlung (Boots belegen ausserhalb BOTTOM einen Slot)."""
    if len(items.slot_items(owned_ids, role=slot_role)) >= ITEM_SLOTS:
        return None
    buckets = items.categorize_gold(owned_ids)["buckets"]
    strongest = max(buckets, key=buckets.get) if any(buckets.values()) else "ap"
    name = {"ad": "Elixir of Wrath", "ap": "Elixir of Sorcery",
            "defense": "Elixir of Iron"}[strongest]
    entry = items.by_name().get(name)
    if not entry:
        return None
    cost = entry[1].get("gold", {}).get("total", 0)
    result = {"item": name, "kind": "consumable", "tag": "Elixier",
              "reason": "Build komplett - Elixier als letzter Power-Spike.",
              "cost": cost, "cost_remaining": cost}
    if current_gold is not None:
        result["current_gold"] = int(current_gold)
        result["affordable"] = current_gold >= cost
    return result


def _pick_next(recs: list[dict], stance: str, owned_names: set[str],
               game_time: float, current_gold: int | None,
               owned_ids: list[int], stance_next: bool = True,
               role: str | None = None, slot_role: str | None = None) -> dict | None:
    """Waehlt aus den Empfehlungen den einen konkreten naechsten Kauf.

    Reihenfolge: bei defensiver Stance und mindestens einem fertigen Item
    zuerst das defensive Situational. Sonst entscheidet die aus den Timelines
    gelernte Kaufposition (avg_slot) zwischen Core und Boots - ohne
    Timeline-Daten gilt die Faustregel: Boots vor dem zweiten fertigen Item.
    Ist das Inventar voll (6 regulaere Slots), wird ein Item-Tausch
    vorgeschlagen statt eines unkaufbaren siebten Items.

    stance_next=False (Ablation "stance"): die Stance-Sonderpfade werden NICHT
    genommen - kein Vorziehen des defensiven Situationals als naechstes Item und
    keine aggressive Boots-/Core-Abweichung. So misst die Stance-Ablation die
    Schicht vollstaendig (nicht nur die drei Score-Gewichte).
    """
    # Effektive Stance fuer die Reihenfolge-Sonderpfade: bei abgeschalteter
    # Schicht wie "balanced" behandeln (kein Sonderpfad).
    eff_stance = stance if stance_next else "balanced"
    if not recs:
        return _elixir_next(owned_ids, current_gold, slot_role=slot_role)
    completed_owned = sum(
        1 for name in owned_names
        if (entry := items.by_name().get(name))
        and entry[1].get("gold", {}).get("total", 0) >= 2000
        and not entry[1].get("into")
    )
    def core_why() -> str:
        if completed_owned == 0:
            return "Erstes fertiges Item - dein wichtigster Power-Spike: "
        return f"Vervollstaendigt deinen Core als {completed_owned + 1}. fertiges Item: "

    boots = next((r for r in recs if r["kind"] == "boots"), None)

    def boots_first(other: dict) -> bool:
        """Kommen Boots vor `other`? avg_slot entscheidet, sonst die Faustregel
        (Boots vor dem zweiten fertigen Item)."""
        if boots is None:
            return False
        if boots.get("avg_slot") and other.get("avg_slot"):
            return boots["avg_slot"] <= other["avg_slot"]
        # Ohne gelernte Reihenfolge: bei Lead den Damage-Spike vor die Boots
        # ziehen (aggressive Deviation), sonst die Faustregel.
        if eff_stance == "aggressive":
            return False
        return completed_owned >= 1 or game_time >= 600

    pick = None
    why_now = ""
    # Anti-Heal wird NICHT als naechster Kauf erzwungen (nur als situatives Item
    # + Alert sichtbar) - der Core-Build hat Vorrang. Es kann hoechstens ueber
    # den defensiven Zweig / die Standard-Situational-Auswahl drankommen.
    if eff_stance == "defensive" and completed_owned >= 1:
        defensive_item = next((r for r in recs
                               if r.get("defensive") and not r.get("antiheal")), None)
        if defensive_item:
            # Boots respektieren die Kaufreihenfolge auch im Defensiv-Zweig -
            # sonst wuerden sie verschluckt.
            if boots and boots_first(defensive_item):
                pick, why_now = boots, "Erst Boots (Kaufreihenfolge), dann defensiv: "
            else:
                pick = defensive_item
                why_now = "Du bist oft gestorben - ein defensiver Kauf sichert ab: "
    if pick is None:
        core = next((r for r in recs if r["kind"] == "core"), None)
        if core and boots:
            if boots_first(core):
                pick, why_now = boots, ("Laut High-Elo-Kaufreihenfolge sind "
                                        "jetzt erst Boots dran: ")
            elif eff_stance == "aggressive" and not core.get("avg_slot"):
                pick, why_now = core, "Du bist vorne - Damage-Spike vor Boots: "
            else:
                pick, why_now = core, core_why()
        elif core:
            pick, why_now = core, core_why()
        elif boots:
            pick, why_now = boots, "Dir fehlen noch Boots: "
    if pick is None:
        pick = recs[0]
        why_now = "Core und Boots sind komplett - staerkstes situatives Item: "

    entry = items.by_name().get(pick["item"])
    cost = entry[1].get("gold", {}).get("total", 0) if entry else 0
    # Teilitems im Inventar (z.B. Sheen fuer Trinity Force) senken den Kaufpreis
    cost_remaining = items.remaining_cost(pick["item"], owned_ids)
    result = {"item": pick["item"], "kind": pick["kind"],
              "tag": pick.get("tag") or _item_tag(pick["item"], role=role),
              "reason": why_now + pick["reason"], "cost": cost,
              "cost_remaining": cost_remaining}

    # Volles Inventar: Ein Kauf ohne Rezept-Ueberschneidung braucht einen
    # freien Slot (Boots nicht - eigener Slot). Dann Item-Tausch vorschlagen;
    # billigstes Slot-Item zuerst, das trifft automatisch Pots/Wards.
    sell_value = 0
    needs_slot = (pick["kind"] != "boots"
                  and items.build_discount(pick["item"], owned_ids) == 0)
    blocking = items.slot_items(owned_ids, role=slot_role)
    if needs_slot and len(blocking) >= ITEM_SLOTS:
        _, victim = min(blocking,
                        key=lambda pair: pair[1].get("gold", {}).get("total", 0))
        sell_value = victim.get("gold", {}).get("sell", 0)
        result["sell_item"] = victim["name"]
        result["sell_value"] = sell_value
        result["reason"] = (f"Inventar voll ({ITEM_SLOTS}/{ITEM_SLOTS}) - "
                            f"Tausch noetig: {victim['name']} verkaufen "
                            f"(+{sell_value} G). ") + result["reason"]

    if current_gold is not None:
        result["current_gold"] = int(current_gold)
        result["affordable"] = current_gold + sell_value >= cost_remaining
    return result


# --- Kaufplan-Leiste (Feature 001) -----------------------------------------
# Flache Liste der naechsten konkreten Kaufschritte fuer das Frontend. Ersetzt
# das frueher hier gepflegte `next_component`-Feld vollstaendig (die Leiste zeigt
# die "jetzt kaufbar"-Information ueber den kumulativen affordable-Ring).

PLAN_CAP = 10   # maximale Anzahl Schritte in der Kaufplan-Leiste (F1)


def _kb_avg_slot(kb: dict, name: str) -> float | None:
    """avg_slot eines Items aus der Champion-KB (core/situational), oder None."""
    for section in (kb.get("core", []), kb.get("situational", [])):
        for e in section:
            if e.get("item") == name and e.get("avg_slot") is not None:
                return e["avg_slot"]
    return None


def _order_components(missing: list[dict], target_iid: int,
                      comp_order: dict, comp_order_global: dict) -> list[dict]:
    """Fehlende Komponenten nach gelernter Kaufreihenfolge sortieren (Kaskade:
    Champion-`component_order` -> `component_order_global` -> Data-Dragon-`from`).
    In der gelernten Order fehlende Komponenten (neue Rezepte) bleiben in
    `from`-Reihenfolge hinten stehen."""
    order = None
    for src in (comp_order, comp_order_global):
        e = (src or {}).get(str(target_iid))
        if e and e.get("order"):
            order = e["order"]
            break
    if not order:
        return list(missing)   # Fallback-Stufe 3: from-Reihenfolge
    rank = {cid: i for i, cid in enumerate(order)}
    big = len(order)
    # Bekannte Komponenten nach gelerntem Rang; unbekannte hinten in from-Ordnung.
    return [m for _, m in sorted(
        enumerate(missing), key=lambda p: rank.get(p[1]["id"], big + p[0]))]


def _finished_unit(name: str, owned_ids: list[int], comp_order: dict,
                   comp_order_global: dict) -> list[tuple[dict, int]]:
    """Schritte fuer EIN fertiges Item: fehlende Komponenten (in gelernter
    Reihenfolge) + das Item selbst. Rueckgabe: Liste (step, internal_cost), wobei
    internal_cost die kumulative Gold-Rechnung speist (Restpreis, nicht der
    Anzeige-Vollpreis)."""
    entry = items.by_name().get(name)
    if not entry:
        return []
    target_iid = entry[0]
    missing, combine_cost = items.direct_components(name, owned_ids)
    missing = _order_components(missing, target_iid, comp_order, comp_order_global)
    out: list[tuple[dict, int]] = []
    for m in missing:
        out.append(({"item_id": m["id"], "name": m["name"], "cost": m["cost"],
                     "final": False}, m["remaining"]))
    out.append(({"item_id": target_iid, "name": name,
                 "cost": entry[1].get("gold", {}).get("total", 0),
                 "final": True}, combine_cost))
    return out


def _next_unit(next_pick: dict, ctx: _RecContext, comp_order: dict,
               comp_order_global: dict) -> list[tuple[dict, int]]:
    """Schritte fuer das Next-Item inkl. Kollaps-Regel: reicht das aktuelle Gold
    fuer das Restrezept (current_gold >= cost_remaining), werden die Komponenten
    weggelassen - der Shop kauft sie beim Direktkauf implizit mit, das Item steht
    selbst an Slot 0."""
    name = next_pick["item"]
    entry = items.by_name().get(name)
    if not entry:
        return []
    full = entry[1].get("gold", {}).get("total", 0)
    remaining = next_pick.get("cost_remaining", full)
    cg = ctx.current_gold
    if cg is not None and cg >= remaining:
        return [({"item_id": entry[0], "name": name, "cost": full,
                  "final": True}, remaining)]
    return _finished_unit(name, ctx.owned_ids, comp_order, comp_order_global)


def _support_upgrade_step(name: str) -> dict:
    """Quest-Upgrade-Schritt fuer eine Support-Endform (F5): kostenlos, kein
    Gold-Ring - die Endform-Wahl laeuft ueber Quest-Fortschritt, nicht ueber Gold."""
    entry = items.by_name().get(name)
    return {"item_id": entry[0] if entry else None, "name": name,
            "cost": 0, "final": True, "upgrade": True}


def _second_next_pick(ctx: _RecContext, next_pick: dict) -> dict | None:
    """Zweites grosses Item der Timeline: die Empfehlung ERNEUT rechnen unter der
    Annahme, dass das Next-Item bereits gekauft wurde (owned + Next-Item). So faellt
    ein Item mit derselben benannten Passive wie das Next-Item automatisch raus -
    `items.conflicts` prueft dann gegen das jetzt 'besessene' Next-Item (z.B. Lich
    Bane vs. Dusk and Dawn, beide Spellblade) - und B ist das echte naechste
    sinnvolle Item statt nur des naechsten Situationals aus der Liste.

    Ruft NUR die Phasen-Helfer (kein `_purchase_plan`/`_assemble_result`) -> keine
    Rekursion. `_conditional_layers` weist seine ctx-Felder neu zu (keine In-place-
    Mutation), darum ist die `replace`-Kopie gegen Kontamination des Original-ctx
    sicher; KB/Quellen werden unveraendert wiederverwendet (kein Neuladen)."""
    name = next_pick.get("item")
    entry = items.by_name().get(name) if name else None
    if not entry:
        return None
    owned2_names = ctx.owned_names | {name}
    owned2_ids = list(ctx.owned_ids) + [entry[0]]
    # has_boots fuer das hypothetische Inventar NEU berechnen: ist das Next-Item
    # die Boots, wuerde der zweite Durchlauf mit dem stale ctx.has_boots (aus dem
    # urspruenglichen Inventar) sonst nochmal Boots empfehlen (doppelte Boots).
    ctx2 = replace(ctx, owned_names=owned2_names, owned_ids=owned2_ids,
                   has_boots=any(items.is_upgraded_boots(n) for n in owned2_names))
    recs2: list[dict] = []
    _core_pick(ctx2, recs2)
    recs2 += _boots_kb(ctx2)
    _conditional_layers(ctx2)
    recs2 += _boots_class(ctx2)
    _score_situationals(ctx2, recs2)
    return _pick_next(recs2, ctx2.stance, owned2_names, ctx2.game_time,
                      ctx2.current_gold, owned2_ids,
                      stance_next=ctx2.weights.stance_next, role=_tag_role(ctx2),
                      slot_role=ctx2.role or ctx2.used_role)


def _purchase_plan(ctx: _RecContext, recs: list[dict], next_pick: dict | None,
                   comp_order_global: dict | None = None,
                   second_pick: dict | None = None) -> list[dict] | None:
    """Baut die Kaufplan-Leiste (Feature 001): fehlende Komponenten des Next-Items
    (gelernte Reihenfolge, Kollaps-Regel) -> Next-Item -> uebernaechstes fertiges
    Item (core/boots/situational, nach avg_slot) mit seinen Komponenten. Die Leiste
    reicht bis einschliesslich dieses zweiten fertigen Items (F1), Support-Upgrade
    zusaetzlich (F5), harte Obergrenze PLAN_CAP.
    `affordable` wird kumulativ von links gegen current_gold gerechnet.

    Bei fehlendem Next-Item oder Elixier-Fall (Build komplett): None."""
    if not next_pick or next_pick.get("kind") == "consumable":
        return None
    comp_order = ctx.kb.get("component_order", {})
    if comp_order_global is None:
        comp_order_global = knowledge.component_order_global()

    support = _support_final(ctx)
    support_name = support[0]["item"] if support else None
    next_name = next_pick["item"]
    next_is_support = bool(support_name) and next_name in items.COMPLETED_SUPPORT_ITEMS

    # steps: (step_dict, internal_cost, is_upgrade)
    steps: list[tuple[dict, int, bool]] = []

    # 1. Next-Item (oder Support-Upgrade, falls das Next-Item eine Endform ist)
    if next_is_support:
        steps.append((_support_upgrade_step(next_name), 0, True))
    else:
        for step, internal in _next_unit(next_pick, ctx, comp_order, comp_order_global):
            steps.append((step, internal, False))

    # 2. Zweites grosses Item (F1: "bis einschliesslich uebernaechstes fertiges
    #    Item"): `second_pick` ist die Empfehlung, die entsteht, wenn man das
    #    Next-Item als bereits gekauft annimmt (in `_assemble_result` via
    #    `_second_next_pick` berechnet). Das liefert das echte naechste sinnvolle
    #    Item B und schliesst ein Item mit derselben Passive wie das Next-Item
    #    automatisch aus (Spellblade-Kollision, z.B. Lich Bane nach Dusk and Dawn).
    #    Der (kostenlose) Support-Upgrade-Schritt (F5) wird avg_slot-basiert daneben
    #    einsortiert und zaehlt nicht als dieses zweite grosse Item.
    future: list[tuple[float | None, str, object]] = []
    if not next_is_support:
        second = second_pick
        b_name = second.get("item") if second else None
        if (b_name and second.get("kind") != "consumable" and b_name != next_name
                and b_name not in items.COMPLETED_SUPPORT_ITEMS):
            # B-Komponenten gegen owned + Next-Item rechnen, damit gemeinsame
            # Rezeptteile nicht doppelt in der Leiste erscheinen.
            owned_after = list(ctx.owned_ids)
            next_entry = items.by_name().get(next_name)
            if next_entry:
                owned_after.append(next_entry[0])
            b_steps = _finished_unit(b_name, owned_after, comp_order,
                                     comp_order_global)
            if b_steps:
                slot = second.get("avg_slot")
                if slot is None:
                    slot = _kb_avg_slot(ctx.kb, b_name)
                future.append((slot, "item", b_steps))
    if support_name and not next_is_support:
        slot = _kb_avg_slot(ctx.kb, support_name)
        # Ohne avg_slot: direkt nach dem Next-Item (ganz vorne unter den Kuenftigen).
        future.append((slot if slot is not None else float("-inf"),
                       "support", support_name))
    # Stabile Sortierung nach avg_slot (None ans Ende).
    future.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0.0))

    for _, kind, payload in future:
        if kind == "support":
            steps.append((_support_upgrade_step(payload), 0, True))
        else:
            for step, internal in payload:
                steps.append((step, internal, False))

    steps = steps[:PLAN_CAP]

    # Kumulatives affordable: current_gold der Reihe nach abziehen (Upgrade-
    # Schritte kosten nichts und lassen die Summe unberuehrt).
    cg = ctx.current_gold
    running = 0
    plan: list[dict] = []
    for step, internal, is_up in steps:
        if not is_up and cg is not None:
            running += internal
            step["affordable"] = cg >= running
        plan.append(step)
    return plan or None
