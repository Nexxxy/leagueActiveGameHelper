"""Boots-Schicht: gemeinsame Boots-Wahl, das Boots-Scoring (V2-03) und der
Eintrag der Boots-Kategorie in den Kandidatenpool.

Aus recommend.py ausgelagert (Modul-Split, T3).
"""

from . import items, knowledge, profiling
from .rec_context import _RecContext, _shrunk, _tag_role
from .rec_explain import _is_defensive, tag_fields
from .rec_path import _int_slot_dist
from .rec_weights import CC_HEAVY_THRESHOLD, RANK_MIN_N, Weights


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
           **tag_fields(chosen["item"], role=role), "avg_slot": chosen.get("avg_slot"),
           # Gelernte Slot-Verteilung der Boots (V2-04). `_pick_next` braucht sie
           # als Escape fuer echte Boots-first-Champions.
           "slot_dist": chosen.get("slot_dist")}
    if source:
        rec["source"] = source
    out.append(rec)
    if chosen["item"] != popular["item"]:
        alt_rec = {"item": popular["item"], "kind": "boots",
                   "reason": alt_reason(popular),
                   **tag_fields(popular["item"], role=role),
                   "avg_slot": popular.get("avg_slot"), "alternative": True}
        if source:
            alt_rec["source"] = source
        out.append(alt_rec)
    return out


def _boots_defensive_want(split: dict, top: dict | None) -> tuple[str | None, str]:
    """(Defensiv-Richtung, Begruendung) der Comp-Regel: 'ad' | 'ap' | None.

    Klar AD-/AP-lastiges Gegnerteam (threat-gewichteter `split`), sonst der
    Top-Threat-Tie-Breaker (ausgeglichenes Team, aber die staerkste Einzel-
    bedrohung ist klar einseitig, Befund D aus review-2026-07-15.md).

    Herausgezogen (V2-03), weil jetzt DREI Stellen dieselbe Definition brauchen:
    die alte Regelkaskade, der Regel-Prior des Score-Modus und der Comp-Hinweis.
    Der Trigger ist damit automatisch threat-gewichtet - ein fed Gegner zaehlt
    ueber `team_damage_split`/`top` mehr als ein nuechterner Farmer."""
    if split["ad"] >= 0.65:
        return "ad", f"Gegnerschaden ist {split['ad']:.0%} AD - Ruestungs-Boots."
    if split["ap"] >= 0.55:
        return "ap", f"Gegnerschaden ist {split['ap']:.0%} AP - MR-Boots."
    if top is not None:
        top_split = top.get("damage_split")
        if top_split:
            if top_split.get("ad", 0) >= 0.6:
                return "ad", (f"Staerkste Bedrohung {top['name']} ist "
                              f"klar AD-lastig - Ruestungs-Boots.")
            if top_split.get("ap", 0) >= 0.6:
                return "ap", (f"Staerkste Bedrohung {top['name']} ist "
                              f"klar AP-lastig - MR-Boots.")
    return None, ""


def _boots_kb(ctx: _RecContext) -> list[dict]:
    """Phase 3, KB-Pfad (Befund S1/D5): Boots aus der Champion-KB waehlen, wenn
    welche gelernt sind und der Spieler noch keine traegt.

    Zwei Modi (s. Weights.boots_kb_factor): die alte Regelkaskade (Faktor 0) oder
    das Scoring aus V2-03. Die situative AD/AP-Wahl der Kaskade gibt es NUR hier."""
    if not ctx.boots_options or ctx.has_boots:
        return []
    if ctx.weights.boots_kb_factor > 0.0:
        return _boots_scored_recs(
            ctx, ctx.boots_options, cells=_boots_cells(ctx), source=None,
            where=f"auf {ctx.champion} {ctx.used_role}")
    split, top, options = ctx.split, ctx.top, ctx.boots_options

    def situational(popular):
        want, want_reason = _boots_defensive_want(split, top)
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
    Boots des Buckets empfehlen - klar gelabelt. Im Score-Modus (V2-03) laeuft
    derselbe Score, nur ohne konditionale Zellen (die Klassen-Aggregate haben
    keine) - dort tragen also allein Pick-Rate und die gedeckelten Regel-Priors."""
    if not ctx.class_boots or ctx.boots_options or ctx.has_boots:
        return []
    label, lrole = ctx.class_label, ctx.lookup_role
    if ctx.weights.boots_kb_factor > 0.0:
        return _boots_scored_recs(
            ctx, ctx.class_boots, cells={}, source="class",
            where=f"in der Klasse {label} {lrole}")
    return _choose_boots(
        ctx.class_boots, ctx.enemy_cc_score, ctx.enemy_profiles,
        popular_reason=lambda c: (f"Meistgespielte Boots der Klasse "
                                  f"({label} {lrole}, {c['pick_rate']:.0%})."),
        cc_reason=lambda m, who: (f"Viel CC im Gegnerteam{who} - {m['item']} "
                                  f"(Tenacity, aus Klassen-Daten {label} {lrole})."),
        alt_reason=lambda popular: (f"Alternative: meistgespielte Boots der Klasse "
                                    f"({label} {lrole}, {popular['pick_rate']:.0%})."),
        source="class", role=_tag_role(ctx))


# --- Boots-Scoring (V2-03, plan_engine_v2.md Konzept 1) ---------------------
# Vorher waehlte die Engine die Boots ueber eine starre Regelkaskade, und die
# Regel schlug die Statistik: Janna bekam gegen ein AD-Gegnerteam Plated
# Steelcaps, obwohl 88 % der Jannas Boots of Swiftness bauen und Steelcaps in
# ihrer Boots-Liste gar nicht vorkommt. Seit V2-02 traegt die KB genug Substanz
# fuer den umgekehrten Weg:
#
#   Score(Boots) = Pick-Rate                       (champion-spezifische Basis)
#                + f * konditionale Schuebe        (boots_by_threat/_cc/_state)
#                + f * gedeckelte Regel-Priors     (viel CC -> Tenacity,
#                                                   AD/AP-Comp -> Konter-Boots)
#
# Damit koennen die Comp-Signale eine klare Champion-Statistik nicht mehr drehen
# (der Prior-Deckel ist kleiner als ein deutlicher Pick-Rate-Abstand) - sie
# bleiben aber sichtbar: als Comp-Hinweis mit beiden Zahlen (_boots_comp_hint).


def _boots_cells(ctx: _RecContext) -> dict:
    """Konditionale Boots-Zellen (V2-02) der Kombi - oder leer.

    Dasselbe Konfidenz-Gate wie bei by_threat/by_state: unterhalb von
    CONF_RICH_MIN sind die situativen Zellen einer Kombi nicht belastbar, und die
    UI sagt genau das ("situative Signale zu duenn"). Die Boots-Schicht faellt
    dann auf Pick-Rate + Regel-Priors zurueck - was fuer den Janna-Fall bereits
    reicht, weil dort die Pick-Rate selbst eindeutig ist."""
    if ctx.confidence != "rich":
        return {}
    return knowledge.boots_cells(ctx.cid, ctx.used_role)


def _cell_total(cell: dict) -> int:
    """Beobachtungszahl einer konditionalen Boots-Zelle: `games` bei
    by_threat/by_cc, `purchases` bei by_state - beides der Nenner der bedingten
    Pick-Rate."""
    return int(cell.get("games") or cell.get("purchases") or 0)


def _boots_cond_boost(cell: dict | None, name: str, pick_rate: float,
                      weights: Weights) -> tuple[float, float | None]:
    """(Schub, bedingte Pick-Rate fuer den Text) einer konditionalen Zelle.

    Zwei Signale, beide mit der Hygiene der Item-Schichten (RANK_MIN_N-Gate,
    Shrinkage, Cap):

    - **Pick-Verschiebung** P(Boots | Lage) gegen die unkonditionierte Pick-Rate.
      Der Nenner dieser Schaetzung ist die ZELLGROESSE (wie viele Spiele/Kaeufe
      fielen in diese Lage), nicht die Zahl der Kaeufe genau dieser Boots -
      darum haengen Gate und Shrinkage daran. Das ist das Signal, das die Frage
      "baut man in DIESER Lage andere Boots?" direkt beantwortet, und es ist
      genau die Groesse, die auch der Backtest misst (was wurde gekauft).
    - **Win-Verschiebung** gegen die Basisrate der Zelle, gegated auf die Zahl
      der Kaeufe dieser Boots (n) - identisch zu by_threat/by_state bei Items.

    Fehlt der Boots in der Zelle (unter der Export-Schwelle der Pipeline), traegt
    sie NICHTS bei: kein stiller Malus aus einer Datenluecke (gleiche Regel wie
    beim next_after-Lift)."""
    if not cell:
        return 0.0, None
    total = _cell_total(cell)
    row = next((i for i in cell.get("items") or [] if i.get("item") == name), None)
    if not row or total <= 0:
        return 0.0, None
    boost = 0.0
    cond_pick = None
    if total >= RANK_MIN_N:
        cond_pick = row.get("pick_rate")
        if cond_pick is None:
            cond_pick = row.get("count", 0) / total
        shrunk = _shrunk(cond_pick, total, pick_rate)
        boost += max(-weights.boots_pick_cap,
                     min(weights.boots_pick_cap, shrunk - pick_rate))
    base_wr = cell.get("base_win_rate")
    n = row.get("count")
    if base_wr is not None and n is not None and n >= RANK_MIN_N:
        wr = _shrunk(row.get("win_rate", base_wr), n, base_wr)
        boost += max(-weights.boots_win_cap,
                     min(weights.boots_win_cap,
                         weights.boots_win_scale * (wr - base_wr)))
    return boost, cond_pick


def _boots_cc_key(enemy_cc_score: float) -> str | None:
    """'cc_heavy' | 'cc_light' | None - Serve-Seite des CC-Buckets, symmetrisch zu
    pipeline/aggregate.py `_cc_bucket` (gleiche Rechnung ueber
    profiling.team_cc_score, gleiche Schwelle). Score 0 heisst "kein einziger
    CC-Prior im Gegnerteam" - dann bucketet auch die Train-Seite nicht."""
    if enemy_cc_score <= 0:
        return None
    return "cc_heavy" if enemy_cc_score >= CC_HEAVY_THRESHOLD else "cc_light"


def _boots_scored(ctx: _RecContext, options: list[dict],
                  cells: dict) -> list[list]:
    """[[score, option, note], ...], absteigend - der Score-Modus der Boots-Wahl."""
    w = ctx.weights
    f = max(0.0, w.boots_kb_factor)
    want, _want_reason = _boots_defensive_want(ctx.split, ctx.top)
    cc_key = _boots_cc_key(ctx.enemy_cc_score)
    cc_heavy = cc_key == "cc_heavy"
    used = []
    if ctx.enemy_bucket:
        used.append(((cells.get("by_threat") or {}).get(ctx.enemy_bucket),
                     f"gegen {ctx.enemy_bucket.upper()}-lastige Teams"))
    if cc_key:
        used.append(((cells.get("by_cc") or {}).get(cc_key),
                     "bei viel Gegner-CC" if cc_heavy else "bei wenig Gegner-CC"))
    if ctx.gold_state:
        used.append(((cells.get("by_state") or {}).get(ctx.gold_state),
                     "wenn du vorne liegst" if ctx.gold_state == "ahead"
                     else "wenn du hinten liegst"))
    rows: list[list] = []
    for opt in options:
        name = opt["item"]
        pick = opt.get("pick_rate") or 0.0
        score, note, best = pick, "", 0.0
        for cell, label in used:
            delta, cond = _boots_cond_boost(cell, name, pick, w)
            score += f * delta
            if cond is not None and abs(delta) > abs(best):
                best, note = delta, f" - {cond:.0%} {label}"
        # Regel-Priors: gemeinsam gedeckelt. Sie duerfen einen deutlichen
        # Pick-Rate-Abstand NICHT mehr ueberstimmen - das war der Kern des
        # Janna-Befunds. Zwei zutreffende Regeln heben den Deckel nicht.
        prior = 0.0
        if cc_heavy and items.is_tenacity_boots(name):
            prior = w.boots_prior_cap
        if want and _is_defensive(name, want):
            prior = w.boots_prior_cap
        score += f * prior
        rows.append([score, opt, note])
    rows.sort(key=lambda r: -r[0])
    return rows


def _boots_comp_hint(ctx: _RecContext, chosen: dict, chosen_pick: float,
                     options: list[dict]) -> dict | None:
    """Comp-Warnhinweis (V2-03): die Comp-Signale sind stark, die Champion-
    Statistik sagt aber etwas anderes.

    Dann verschwindet das Signal nicht (das war der Fehler der alten Kaskade in
    die andere Richtung), sondern wird ein EIGENER situativer Vorschlag mit
    BEIDEN Zahlen - "78 % Swiftness, gegen 64 % AD waere Steelcaps denkbar (7 %
    bauen das)". Der Hauptvorschlag bleibt unangetastet."""
    if not ctx.weights.boots_comp_hint:
        return None
    want, want_reason = _boots_defensive_want(ctx.split, ctx.top)
    cc_heavy = _boots_cc_key(ctx.enemy_cc_score) == "cc_heavy"
    cand = trigger = None
    if cc_heavy:
        cand = next((b["item"] for b in options
                     if items.is_tenacity_boots(b["item"])), None)
        cand = cand or items.standard_defensive_boots("ap")
        who = ", ".join(profiling.cc_threats(ctx.enemy_profiles))
        trigger = f"viel CC im Gegnerteam ({who})" if who else "viel CC im Gegnerteam"
    elif want:
        cand = next((b["item"] for b in options
                     if _is_defensive(b["item"], want)), None)
        cand = cand or items.standard_defensive_boots(want)
        # "Gegnerschaden ist 91% AD" / "Staerkste Bedrohung Zed ist klar AD-lastig"
        trigger = want_reason.split(" - ")[0].rstrip(".")
    if not cand or not trigger or cand == chosen["item"]:
        return None
    if not items.is_valid_sr(cand):
        return None
    row = next((b for b in options if b["item"] == cand), None)
    share = (f"{row['pick_rate']:.0%} bauen das" if row and row.get("pick_rate")
             else "praktisch niemand baut das")
    return {"item": cand, "kind": "boots", "alternative": True, "hint": True,
            **tag_fields(cand, role=_tag_role(ctx)),
            "avg_slot": row.get("avg_slot") if row else None,
            "reason": (f"Hinweis: {chosen_pick:.0%} bauen {chosen['item']} - "
                       f"{trigger}: da waere {cand} denkbar ({share}).")}


def _boots_scored_recs(ctx: _RecContext, options: list[dict], *, cells: dict,
                       source: str | None, where: str) -> list[dict]:
    """Boots-Empfehlungen im Score-Modus: Hauptvorschlag (bester Score), die
    meistgespielten als `alternative` (falls abweichend) und der Comp-Hinweis
    (falls die Comp etwas anderes nahelegt). Maximal drei Eintraege, der
    Hauptvorschlag steht IMMER vorn - `_pick_next` nimmt genau den."""
    rows = _boots_scored(ctx, options, cells)
    if not rows:
        return []
    _score, chosen, note = rows[0]
    popular = options[0]
    pick = chosen.get("pick_rate") or 0.0
    role = _tag_role(ctx)
    lead = ("Meistgespielte Boots" if chosen["item"] == popular["item"]
            else "Statistisch beste Boots in dieser Lage")
    rec = {"item": chosen["item"], "kind": "boots",
           "reason": f"{lead} {where} ({pick:.0%} Pick){note}.",
           **tag_fields(chosen["item"], role=role),
           "avg_slot": chosen.get("avg_slot"),
           # Kauf-Timing (V2-02): Minuten-Quantile der Boots-Fertigstellung.
           # `_pick_next` nutzt sie als gelernten Ersatz fuer die 10-Minuten-
           # Faustregel, wenn keine avg_slot-Reihenfolge vorliegt.
           "minute": chosen.get("minute"),
           # Gelernte Slot-Verteilung der Boots (V2-04): entscheidet in
           # `_pick_next`, ob dieser Champion die Boots wirklich als ERSTEN
           # fertigen Kauf schnuert.
           "slot_dist": chosen.get("slot_dist")}
    if source:
        rec["source"] = source
    out = [rec]
    if chosen["item"] != popular["item"]:
        alt = {"item": popular["item"], "kind": "boots",
               "reason": (f"Alternative: meistgespielte Boots {where} "
                          f"({popular.get('pick_rate', 0):.0%})."),
               **tag_fields(popular["item"], role=role),
               "avg_slot": popular.get("avg_slot"), "alternative": True}
        if source:
            alt["source"] = source
        out.append(alt)
    hint = _boots_comp_hint(ctx, chosen, pick, options)
    if hint and all(hint["item"] != r["item"] for r in out):
        if source:
            hint["source"] = source
        out.append(hint)
    return out


# --- Boots im Kandidatenpool (Revision nach dem Gate-Fail) ------------------
# Erster Anlauf der gewichteten Liste stellte alles ohne Pool-Score ans Ende -
# Boots also hinter bis zu SITUATIONAL_SHOWN + 1 Karten. Der Vergleichslauf
# (je 165 438 Samples) zeigte, was das kostet: Boots-Hit@3 72,7 % -> 40,8 %
# (-31,9 pp) bei EXAKT unveraenderter Boots-Wahl - reine Listenposition, und
# Boots-Kaeufe sind der haeufigste Abweichungsfall (Supports am haertesten).
#
# Boots stehen darum jetzt auf der Pool-Skala, aber als KATEGORIE ("irgendwelche
# Boots"), nicht als Einzelitem:
#
#   Score(Boots) = SUM(pick_rate ueber alle boots_options)
#                * Slot-Support(aktueller Slot) auf der pick-gewichteten
#                  Merge-`slot_dist` aller boots_options
#
# Die Summe ist der Punkt: bei gesplitteter Boots-Wahl (Swiftness 45 % /
# Steelcaps 40 %) waere die Kategorie sonst kuenstlich schwach, obwohl 85 % der
# Spieler in diesem Slot IRGENDWELCHE Boots schnueren. Bewusst NICHT dabei:
# der next_after-Lift (das Bigramm-Modell kennt keine Boots-Uebergaenge - ein
# neutraler Faktor ist ehrlicher als ein erfundener) und die Threat-/State-
# Schuebe (die stecken bereits in der V2-03-Boots-WAHL, ein zweites Mal waeren
# sie doppelt gezaehlt).
#
# WIRKUNG NUR AUF DIE ANZEIGE: Der Score bestimmt Listenrang und `now_rel`,
# nie den naechsten Kauf - `_path_winner` ueberspringt `kind=="boots"` (wie
# Anti-Heal), die gewachsenen `_pick_next`-Gates (hartes Boots-Gate vor dem
# ersten Legendary, avg_slot-Vergleich, Defensiv-Zweig) bleiben allein zustaendig.


def _boots_slot_merge(options: list[dict]) -> tuple[float, dict[int, float]]:
    """(Summe der Pick-Rates, pick-gewichtete Merge-`slot_dist`) der Boots-
    KATEGORIE - die gemeinsame Datenbasis von Pool-Score und Anzeige-Regel.

    Die KB-weite Slot-Tabelle (`ctx.slot_dist`) deckt nur core/situational ab
    (s. `knowledge.slot_dist`); die Boots tragen ihre Verteilung einzeln am
    Options-Eintrag. Ohne jede Verteilung ist das zweite Element leer - dann gibt
    es fuer die Kategorie schlicht kein Slot-Datum.

    `options` ist die Liste, aus der die angezeigten Boots stammen: die
    Champion-Boots (`ctx.boots_options`) oder - wenn der Champion selbst keine
    hat - die Klassen-Boots. Sonst haetten ausgerechnet die Klassen-Boots nie
    ein Slot-Datum und liefen an der Anzeige-Regel vorbei."""
    pick_total = 0.0
    merged: dict[int, float] = {}
    weight_total = 0.0
    for opt in options:
        try:
            pick = float(opt.get("pick_rate") or 0.0)
        except (TypeError, ValueError):
            pick = 0.0
        pick_total += pick
        clean = _int_slot_dist(opt.get("slot_dist"))
        if pick <= 0.0 or not clean:
            continue
        weight_total += pick
        for slot, share in clean.items():
            merged[slot] = merged.get(slot, 0.0) + pick * share
    if pick_total <= 0.0 or not merged:
        return pick_total, {}
    # Normieren, damit die Merge-Verteilung wieder Anteile sind (fuer den
    # now/peak-Quotienten irrelevant, aber so bleibt sie interpretierbar).
    return pick_total, {slot: share / weight_total for slot, share in merged.items()}


def _boots_pool_score(ctx: _RecContext, primary: dict) -> float | None:
    """Pool-Score der Boots-KATEGORIE - oder None, wenn es keine Slot-Daten gibt.

    Slot-Support analog zu `_slot_support`, aber auf der pick-gewichteten
    Merge-Verteilung der Boots-Optionen statt auf `ctx.slot_dist`: die
    KB-weite Slot-Tabelle deckt nur core/situational ab (s.
    `knowledge.slot_dist`), die Boots tragen ihre Verteilung einzeln am
    Options-Eintrag. Zwei Abweichungen vom Item-Fall, beide bewusst:

    * **Kein Rueckfall auf `avg_slot`/SLOT_NO_DATA_FIT.** Ohne jede
      Merge-Verteilung gibt es keinen Score (None) - die Boots bleiben dann
      Nicht-Pool wie vor dieser Revision. Ein erfundener Kategorie-Score waere
      genau die Vergleichbarkeit, die `now_rel` sonst vermeidet.
    * **Kein Hard-Drop hinter dem Datenende** (`slot_late_keep`). Der Drop
      existiert, damit ein veraltetes Core-Item nicht als naechster Kauf
      weiterempfohlen wird - Boots koennen ueber den Pool ohnehin nie `next`
      werden, und wer in Slot 6 noch keine Boots traegt, soll sie sehen. Der
      Faktor ist dort per Konstruktion schon maximal gedaempft, die Boots
      landen also am Ende der Pool-Gruppe.

    Neutral (Faktor 1.0) bleibt wie bei `_slot_support` der Fall, dass der
    aktuelle Slot jenseits des Datenhorizonts der Kombi liegt: dort ist jede
    Aussage weggeprunt, und dann darf die Kategorie nicht als einzige gedaempft
    werden, waehrend alle Item-Kandidaten neutral laufen."""
    pick_total, merged = _boots_slot_merge(ctx.boots_options)
    if pick_total <= 0.0 or not merged:
        return None
    if ctx.cur_slot > ctx.slot_horizon:
        return pick_total
    peak = max(merged.values()) or 1.0
    now = merged.get(ctx.cur_slot, 0.0)
    mult = 1.0 + ctx.weights.path_rescore_factor * (now / peak - 1.0)
    return pick_total * mult


def _boots_pool_entry(ctx: _RecContext, recs: list[dict]) -> None:
    """Traegt den Boots-Kategorie-Score in `ctx.path_scores` ein, damit sich die
    empfohlenen Boots regulaer in die gewichtete Liste einordnen.

    Gilt NUR fuer den Primaervorschlag - den ersten `kind=="boots"`-Eintrag, der
    weder `alternative` noch Klassen-Fallback ist. Die Alternative (und der
    Comp-Hinweis) bleiben ohne Score in der Rest-Gruppe: sie sind bewusst
    abgedimmte Zweitoptionen und koennen so auch nie vor den Primaervorschlag
    rutschen (die Boots-Metrik liest den ERSTEN boots-Eintrag).

    Im Legacy-Modus (`path_rescore_factor == 0`) gibt es keinen Pool - dann
    bleibt alles wie zuvor."""
    if ctx.weights.path_rescore_factor <= 0.0:
        return
    primary = next((r for r in recs if r.get("kind") == "boots"), None)
    if (primary is None or primary.get("alternative")
            or primary.get("source") == "class"):
        return
    score = _boots_pool_score(ctx, primary)
    if score is None:
        return
    # Neu zuweisen statt mutieren - gleiche Regel wie in `_score_situationals`:
    # `_second_next_pick` arbeitet auf einer `replace`-Kopie des ctx.
    ctx.path_scores = {**ctx.path_scores, primary["item"]: score}
