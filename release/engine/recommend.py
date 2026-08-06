"""Empfehlungs-Engine: Wissensbasis + Gegner-Profile + eigene Performance -> Items.

Orchestrator und Kompatibilitaets-Fassade: `recommend()` baut den Lauf-Kontext
(`_build_context`) und ruft die Phasen-Helfer der Themen-Module in fester
Reihenfolge - Gewichte/Kontext (rec_weights, rec_context), Stance und Archetyp
(rec_stance, rec_archetype), Core-Pfad (rec_path, rec_next_after), Boots
(rec_boots), situative Schichten inkl. Support-Endwahl (rec_situational),
Anti-Heal (rec_antiheal), Begruendungstexte (rec_explain), Anzeige-Schicht
(rec_display) sowie Kauf-Auswahl und Kaufplan-Leiste (rec_plan).

Stance-Logik:
  struggling (schlechte KDA / viele Tode)  -> defensiv
  ahead (gute KDA und Gold-Vorsprung)      -> aggressiv
  sonst                                    -> ausgewogen
"""

from . import champions, items, knowledge, profiling
# Fassade: die Themen-Helfer liegen in eigenen Modulen, werden hier aber unter
# ihren alten Namen re-exportiert. Tests und pipeline/backtest.py greifen massiv
# auf `recommend.<name>` zu (auch auf die Unterstrich-Namen und Konstanten) -
# darum durchgehend das noqa: F401.
from .rec_weights import (  # noqa: F401  Fassade, Modul-Split T1
    CC_HEAVY_THRESHOLD, CONF_RICH_MIN, DEFAULT_WEIGHTS, RANK_MIN_N, SHRINK_K,
    Weights,
)
from .rec_context import (  # noqa: F401  Fassade, Modul-Split T1
    _REDUNDANT_TAGS, _RecContext, _enemy_damage_bucket, _redundant_stack,
    _shrunk, _spike_warnings, _synergy_boost, _tag_role, confidence_tier,
)
from .rec_stance import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    STATE_LEAD_GOLD,
    fielded_lead, earned_lead, lead_note, own_stance, _stance_note,
)
from .rec_archetype import _select_archetype  # noqa: F401  Fassade (T2)
from .rec_explain import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _bucket_label, explain_item, _item_tag, _is_defensive, _is_defensive_item,
    tag_fields,
)
from .rec_antiheal import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _my_damage_type, _antiheal_recommendation,
)
from .rec_next_after import (  # noqa: F401  Fassade, Modul-Split T2
    NEXT_AFTER_NOTE_MIN_RATIO, _next_after_lift, _next_after_model,
    _next_after_reason, _owned_completed,
)
from .rec_path import (  # noqa: F401  Fassade, Modul-Split T2
    SLOT_LATE_TOLERANCE, SLOT_NO_DATA_FIT, _core_pick, _core_pick_path,
    _core_reason, _core_rec, _current_slot, _degraded_core_note,
    _int_slot_dist, _learned_conflict, _slot_horizon, _slot_support,
)
from .rec_boots import (  # noqa: F401  Fassade, Modul-Split T3
    _boots_cc_key, _boots_cells, _boots_class, _boots_comp_hint,
    _boots_cond_boost, _boots_defensive_want, _boots_kb, _boots_pool_entry,
    _boots_pool_score, _boots_scored, _boots_scored_recs, _boots_slot_merge,
    _cell_total, _choose_boots,
)
from .rec_situational import (  # noqa: F401  Fassade, Modul-Split T4/T6
    DEF_SLOT_MIN_N, SITUATIONAL_SHOWN, _behind_row, _conditional_layers,
    _death_note, _def_rank, _def_usable, _def_want, _defensive_layer_active,
    _defensive_rec, _partner_adjust, _partner_layer_active, _pool_def_row,
    _reserve_defensive, _score_situationals, _support_final,
)
from .rec_display import (  # noqa: F401  Fassade, Modul-Split T5
    _POOL_EXCLUDED_FLAGS, _boots_gate_open, _boots_slot_supported,
    _boots_visible, _completed_legendaries, _defensive_bridge, _display_blocked,
    _display_order, _next_only_filter, _now_rel, _path_winner, _pool_excluded,
)
from .rec_plan import (  # noqa: F401  Fassade, Modul-Split T6
    ITEM_SLOTS, PLAN_CAP, _elixir_next, _finished_unit, _kb_avg_slot,
    _next_unit, _order_components, _pick_next, _purchase_plan,
    _second_next_pick, _support_upgrade_step,
)


def _build_context(champion: str, role: str | None, owned_names: set[str],
                   my_scores: dict, enemy_profiles: list[dict],
                   game_time: float, current_gold: int | None,
                   owned_ids: list[int] | None, my_level: int,
                   ally_items: set[str] | None, weights: Weights,
                   champion_id: str | None,
                   ally_gold_spent: int | None = None,
                   bot_partner: dict | None = None,
                   death_signal: dict | None = None) -> _RecContext:
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
    # Schadenstyp-Bucket des Gegnerteams (Train-Definition). Lag frueher in
    # _conditional_layers; die Boots-Schicht (V2-03) laeuft VOR dieser Phase und
    # braucht denselben Bucket fuer ihre boots_by_threat-Zelle - gleiche
    # Rechnung, nur eine Phase frueher.
    enemy_bucket = _enemy_damage_bucket(enemy_profiles)
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

    # next_after-Bigramm (T2) einmal pro Lauf auswerten - Core-Pick (V2-05) und
    # Situational-Scoring greifen auf dasselbe Modell zu. Bei abgeschalteter
    # Schicht wird gar nichts erst gerechnet.
    na_block = knowledge.next_after(cid, used_role)
    na_cond, na_marginal = (_next_after_model(na_block)
                            if weights.next_after_factor else ({}, {}))
    na_owned = _owned_completed(owned_ids or []) if na_cond else []
    # Restpfad-Neubewertung (V2-05): Slot-Support und gelernte Exklusivitaet
    # nur laden, wenn die jeweilige Schicht ueberhaupt aktiv ist.
    slot_dist = (knowledge.slot_dist(cid, used_role)
                 if weights.path_rescore_factor > 0.0 else {})
    exclusive = (knowledge.exclusive_pairs(cid, used_role)
                 if weights.learned_exclusive else [])

    return _RecContext(
        champion=champion, used_role=used_role, role=role, cid=cid,
        owned_names=owned_names, owned_ids=owned_ids or [],
        enemy_profiles=enemy_profiles, ally_items=ally_items or set(),
        game_time=game_time, current_gold=current_gold, weights=weights,
        bot_partner=bot_partner, death_signal=death_signal,
        kb=kb, top=top, split=split, enemy_cc_score=enemy_cc_score,
        enemy_bucket=enemy_bucket,
        fielded_lead=f_lead, earned_lead=e_lead,
        gold_state=gold_state, stance=stance, stance_reason=stance_reason,
        build=build, build_reason=build_reason, core_source=core_source,
        situational_source=situational_source, confidence=confidence_tier(kb),
        has_boots=has_boots, boots_options=boots_options,
        # Uebergangs-Bigramm der Kombi (T2). Ueber den knowledge-Accessor, damit
        # alte builds.yaml ohne den Block sauber leer liefern.
        next_after=na_block, na_cond=na_cond, na_marginal=na_marginal,
        na_owned=na_owned,
        # Restpfad-Neubewertung (V2-05). Beide Accessors liefern fuer KBs vor
        # V2-04 leer - dann bleibt der Slot-Support neutral (kein Ausschluss)
        # und die gelernte Exklusivitaet stumm.
        slot_dist=slot_dist, slot_horizon=_slot_horizon(slot_dist),
        cur_slot=_current_slot(owned_ids, has_boots),
        exclusive=exclusive)


def _assemble_result(ctx: _RecContext, recs: list[dict],
                     antiheal: dict | None) -> dict:
    """Phase 7 (Befund S1): naechster Kauf und Result-Dict inkl.
    Confidence-Notes."""
    kb = ctx.kb
    # Boots als Kategorie in den Pool heben - reine Anzeige-/`now_rel`-Wirkung.
    # Die Next-Wahl darunter sieht davon nichts: `_path_winner` ueberspringt
    # `kind=="boots"`, und in `ctx.path_block` landet der Eintrag nie.
    _boots_pool_entry(ctx, recs)
    next_pick = _pick_next(recs, ctx.stance, ctx.owned_names, ctx.game_time,
                           ctx.current_gold, ctx.owned_ids,
                           role=_tag_role(ctx),
                           slot_role=ctx.role or ctx.used_role,
                           next_block=ctx.path_block,
                           path_first=_path_winner(ctx, recs))
    # Anzeige-Regel "nur plausible naechste Kaeufe" (plan_next_item_only.md):
    # AB HIER arbeitet alles Weitere auf der gefilterten Menge. Bewusst NACH
    # `_pick_next`: die Kaufreihenfolge entscheidet unveraendert auf allen
    # Kandidaten (der Filter ist eine Anzeige-Regel, keine Auswahlregel), und der
    # Sieger bleibt dadurch garantiert sichtbar.
    recs = _next_only_filter(ctx, recs, next_pick)
    # Relative Kaufstaerke und Defensiv-Bezug haengen beide am fertigen
    # Next-Pick, darum erst hier (recs sind dieselben Dicts wie result["items"]).
    _now_rel(ctx, recs, next_pick)
    _defensive_bridge(recs, next_pick, items.count_completed(ctx.owned_ids))
    result = {
        "role": ctx.used_role,
        "stance": ctx.stance,
        "stance_reason": ctx.stance_reason,
        # Klarstellung, dass die Item-Empfehlung trotz defensiver Stance der
        # gelernten Kaufreihenfolge folgt (Befund H, review-2026-07-15.md /
        # Befund D, 2026-07-13). Leer, wenn die Stance nicht defensiv ist.
        "stance_note": _stance_note(ctx.stance),
        "enemy_damage_split": ctx.split,
        "enemy_cc_score": ctx.enemy_cc_score,
        # Todes-Signal aus dem Kill-Feed (V2-08) - None, solange nichts scharf
        # ist oder gar keine Events vorliegen (alte Dumps/Backtest/Demo).
        "death_signal": ctx.death_signal,
        "knowledge_games": kb.get("games", 0),
        "confidence": ctx.confidence,
        "build": ctx.build.get("name") if ctx.build else None,
        "build_reason": ctx.build_reason,
        "builds_available": [b["name"] for b in kb.get("builds", [])],
        "antiheal": antiheal,
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
    # Anzeige-/Mess-Reihenfolge erst JETZT festlegen: `_pick_next`, `_now_rel`
    # und `_purchase_plan` arbeiten ueber Namen bzw. Pool-Scores und sind von
    # der Listenposition unabhaengig - die Umsortierung darf sie also nicht
    # mehr sehen. Es sind dieselben Dicts, nur neu geordnet.
    result["items"] = _display_order(ctx, recs)
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


def recommend(champion: str, role: str | None, owned_names: set[str],
              my_scores: dict, enemy_profiles: list[dict],
              game_time: float = 0.0, current_gold: int | None = None,
              owned_ids: list[int] | None = None, my_level: int = 0,
              ally_items: set[str] | None = None,
              weights: Weights = DEFAULT_WEIGHTS,
              champion_id: str | None = None,
              ally_gold_spent: int | None = None,
              bot_partner: dict | None = None,
              death_signal: dict | None = None) -> dict:
    """Orchestrator (Struktur-Review 2026-07-17 T3, Befund S1): baut den Kontext
    auf und ruft die Phasen-Helfer in fester Reihenfolge - Core-Pick, Boots
    (KB- und Klassen-Pfad), konditionale Schichten, situatives Scoring,
    Anti-Heal, Result-Assembly. Die eigentliche Logik liegt in den _*-Helfern."""
    ctx = _build_context(champion, role, owned_names, my_scores, enemy_profiles,
                         game_time, current_gold, owned_ids, my_level, ally_items,
                         weights, champion_id, ally_gold_spent, bot_partner,
                         death_signal)
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
