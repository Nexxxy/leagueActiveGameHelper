"""Situative Schicht: konditionale Layer (by_threat/by_state/by_partner),
Botlane-Partner-Glue, Behind-Situationals (V2-08), das Scoring des situativen
Blocks und die Support-Item-Endwahl (Schicht 5).

Aus recommend.py ausgelagert (Modul-Split, T4/T6).
"""

from . import champions, items, knowledge
from .rec_boots import _boots_defensive_want
from .rec_context import (
    _RecContext, _redundant_stack, _shrunk, _synergy_boost, _tag_role,
)
from .rec_explain import (_bucket_label, _is_defensive, _is_defensive_item,
                          explain_item, tag_fields)
from .rec_next_after import _next_after_lift, _next_after_reason
from .rec_path import _learned_conflict, _slot_support
from .rec_weights import RANK_MIN_N


def _conditional_layers(ctx: _RecContext) -> None:
    """Phase 4 (Befund S1): by_threat-/by_state-Schichten vorbereiten inkl.
    Konfidenz-Gate und Klassen-Fallback-Datenbeschaffung. Fuellt die
    konditionalen und Klassen-Felder des ctx."""
    kb = ctx.kb
    # Threat-konditionierte Item-Win-Rates (Schicht 4, datengetrieben): gegen
    # ein klar AD- bzw. AP-lastiges Gegnerteam zaehlt, was empirisch gewonnen hat.
    # WICHTIG (Review G): Der by_threat-Lookup nutzt die TRAIN-Definition -
    # ungewichtetes Mittel der Champion-Priors (`_enemy_damage_bucket`, seit
    # V2-03 bereits in _build_context berechnet), NICHT das threat-gewichtete
    # `split`. Nur so werden die gelernten Zellen unter derselben Definition
    # abgefragt, unter der sie gezaehlt wurden; `split` bleibt fuer Boots-/
    # Defensiv-Texte unveraendert threat-gewichtet.
    enemy_bucket = ctx.enemy_bucket
    bt = kb.get("by_threat", {}).get(enemy_bucket) if enemy_bucket else None
    # Volle Item-Dicts behalten (count/win_rate) + Basisrate des Buckets fuer
    # Shrinkage und Ranking-Gate. base_win_rate kann bei kuratierten Overrides
    # oder alter KB fehlen -> dann Kuratiert-Pfad (kein Gate, keine Shrinkage).
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


# --- Behind-Situationals + Todes-Signal (V2-08, plan_engine_v2.md Konzept 3) -
# Der Hauptvorschlag (`next`) bleibt ehrlich: die Mehrheit gewinnt, auch wenn das
# bei Rueckstand Rabadon ist (abgestimmte Design-Entscheidung, plan_engine_v2.md
# Abschnitt 2). Was sich aendert, ist der situative BLOCK: bei defensiver Stance
# oder scharfem Todes-Signal (rec_deaths.death_signal) bekommt er mindestens
# einen Slot fuer eine defensive Option - statt drei Glass-Cannon-Items an einen
# Spieler auszuliefern, der gerade dreimal von demselben Gegner gestorben ist.
#
# Quellen-Kaskade, jede Stufe mit EHRLICHER Kennzeichnung im Begruendungstext:
#   1. Champion-Behind-Zelle (`by_state.behind`, V2-07) ab DEF_SLOT_MIN_N Kaeufen
#   2. Klassen-Behind-Fallback (`knowledge.class_by_state`, V2-07) - klar als
#      Klassen-Daten gelabelt (`source: "class"`)
#   3. DEF_TAGS-Overlay aus dem Champion-Pool: "zu duenn fuer Behind-Statistik,
#      aber defensiv - X % Pick, Y % Win global"

# Mindest-`count` einer Behind-Zelle, damit sie als CHAMPION-eigene Evidenz
# durchgeht. Gleiche Groessenordnung wie OK_STATE_MIN_N im Post-Game-Check
# (app/postgame/build_replay.py): der Pipeline-Cutoff MIN_STATE_ITEM = 5 laesst
# Zellen zu, die fuer eine Empfehlung zu duenn sind - Gwens eigene Behind-Zelle
# fuehrt Zhonya mit 8 und Riftmaker mit 9 Kaeufen. Unter dieser Schwelle ist der
# Klassen-Fallback (ap_fighter JUNGLE: Zhonya n=45) die ehrlichere Quelle.
DEF_SLOT_MIN_N = 10


def _defensive_layer_active(ctx: _RecContext) -> bool:
    """Feuert die Slot-Reservierung? Defensive Stance ODER scharfes Todes-Signal
    - und nur, solange der Schalter `defensive_slot` an ist (Ablation)."""
    if not ctx.weights.defensive_slot:
        return False
    return ctx.stance == "defensive" or bool(ctx.death_signal)


def _def_want(ctx: _RecContext) -> str | None:
    """'ad' | 'ap' | None - gegen welchen Schadenstyp die defensive Option
    zaehlen soll. Das Todes-Signal hat Vorrang (es misst, woran der Spieler
    TATSAECHLICH stirbt), sonst die bestehende Comp-Regel."""
    sig = ctx.death_signal or {}
    if sig.get("damage_type") in ("ad", "ap"):
        return sig["damage_type"]
    want, _reason = _boots_defensive_want(ctx.split, ctx.top)
    return want


def _def_rank(name: str, want: str | None) -> int:
    """Sortierrang eines defensiven Kandidaten: 0 = passende Resistenz
    (Ruestung gegen AD, MR gegen AP), 1 = nur HP (hilft gegen beides, aber
    unspezifisch), 2 = alles Uebrige."""
    tags = items.tags_of(name)
    if want == "ad":
        key = "Armor"
    elif want == "ap":
        key = "SpellBlock"
    else:
        return 0 if tags & items.DEF_TAGS else 2
    if key in tags:
        return 0
    return 1 if "Health" in tags else 2


def _def_usable(ctx: _RecContext, name: str, taken: set[str]) -> bool:
    """Dieselben harten Filter wie ueberall (besessen, geteilte Passive,
    gelernte Exklusivitaet, SR-Gueltigkeit) plus: nicht schon im Block."""
    return (name not in taken and name not in ctx.owned_names
            and not items.conflicts(name, ctx.owned_names)
            and not _learned_conflict(ctx, name)
            and items.is_valid_sr(name))


def _behind_row(ctx: _RecContext, cell: dict, taken: set[str],
                want: str | None, min_n: int) -> dict | None:
    """Bester defensiv markierter Eintrag einer `by_state.behind`-Zelle ab
    `min_n` Kaeufen - passende Resistenz zuerst, dann die haeufigere Zelle.

    Das `defensive`-Flag der Aggregation ist nur der grobe VORFILTER (es markiert
    ueber `items.DEF_TAGS`, also inklusive blosser Health). Geprueft wird
    zusaetzlich mit `_is_defensive_item`, damit ein Schadens-Item mit
    HP-Nebenstat (Liandry's Torment, Riftmaker) nicht die Defensiv-Option
    stellt."""
    rows = [r for r in (cell or {}).get("items") or []
            if r.get("defensive") and int(r.get("count") or 0) >= min_n
            and _is_defensive_item(r.get("item", ""))
            and _def_usable(ctx, r.get("item", ""), taken)]
    if not rows:
        return None
    rows.sort(key=lambda r: (_def_rank(r["item"], want), -int(r.get("count") or 0)))
    return rows[0]


def _pool_def_row(ctx: _RecContext, taken: set[str],
                  want: str | None) -> dict | None:
    """Letzte Stufe: defensiv getaggtes Item aus dem Champion-Pool selbst
    (core + situational). Ohne Behind-Statistik, dafuer mit den globalen Zahlen
    des Champions - und genau so wird es im Text auch ausgewiesen.

    Gefiltert wird mit `_is_defensive_item` statt mit `items.DEF_TAGS`: der
    Pool eines AP-Fighters ist voll von Schadens-Items mit HP-Nebenstat, und die
    sind als "Defensiv-Option" schlicht gelogen."""
    rows = [e for e in list(ctx.situational_source) + list(ctx.core_source)
            if _is_defensive_item(e.get("item", ""))
            and _def_usable(ctx, e.get("item", ""), taken)]
    if not rows:
        return None
    rows.sort(key=lambda e: (_def_rank(e["item"], want),
                             -(e.get("pick_rate") or 0.0)))
    return rows[0]


def _death_note(ctx: _RecContext, name: str) -> str:
    """Personalisierter Vorsatz aus dem Kill-Feed ("3x von Viego gestorben -
    Zhonya's Hourglass macht dich ueberlebensfaehiger.") oder leer."""
    sig = ctx.death_signal
    if not sig or not sig.get("reason"):
        return ""
    return f"{sig['reason']} - {name} macht dich ueberlebensfaehiger. "


def _defensive_rec(ctx: _RecContext, taken: set[str]) -> dict | None:
    """Die beste defensive Option fuer den reservierten Slot - oder None, wenn
    keine Quelle etwas hergibt (dann bleibt der Block unveraendert)."""
    want = _def_want(ctx)
    source = None
    avg_slot = None

    row = _behind_row(ctx, (ctx.kb.get("by_state") or {}).get("behind") or {},
                      taken, want, DEF_SLOT_MIN_N)
    if row:
        name = row["item"]
        reason = (f"Defensiv-Option bei Rueckstand: {name} wird auf "
                  f"{ctx.champion} {ctx.used_role} hinten {int(row['count'])}x "
                  f"gekauft ({row.get('win_rate', 0.0):.0%} Win).")
    else:
        bucket = ctx.class_bucket or champions.bucket_for_id(ctx.cid)
        lrole = ctx.lookup_role or ctx.used_role or ctx.role or ""
        cell = (knowledge.class_by_state(bucket, lrole) or {}).get("behind") or {}
        row = _behind_row(ctx, cell, taken, want, DEF_SLOT_MIN_N)
        if row:
            name = row["item"]
            label = _bucket_label(bucket)
            source = "class"
            reason = (f"Defensiv-Option bei Rueckstand aus Klassen-Daten: "
                      f"{label} {lrole} bauen hinten {name} "
                      f"(n={int(row['count'])}, {row.get('win_rate', 0.0):.0%} "
                      f"Win) - die Behind-Zelle von {ctx.champion} ist dafuer "
                      f"zu duenn.")
        else:
            row = _pool_def_row(ctx, taken, want)
            if not row:
                return None
            name = row["item"]
            avg_slot = row.get("avg_slot")
            reason = (f"Defensiv-Option: zu duenn fuer Behind-Statistik, aber "
                      f"defensiv - {row.get('pick_rate', 0.0):.0%} Pick, "
                      f"{row.get('win_rate', 0.0):.0%} Win global.")

    rec = {"item": name, "kind": "situational",
           **tag_fields(name, role=_tag_role(ctx)),
           "reason": _death_note(ctx, name) + reason,
           "defensive": True,
           # Markierung fuer Frontend/Report: dieser Eintrag steht hier, weil ein
           # Slot fuer eine defensive Option reserviert wurde - nicht, weil er
           # den Score-Wettbewerb gewonnen hat.
           "defensive_slot": True,
           "avg_slot": avg_slot}
    if source:
        rec["source"] = source
    return rec


def _reserve_defensive(ctx: _RecContext, recs: list[dict],
                       chosen: list[dict]) -> list[dict]:
    """Reserviert im situativen Block einen Platz fuer die defensive Option.

    Ist bereits ein defensives Item unter den gewaehlten Kandidaten, bleibt der Block
    unveraendert - dann bekommt es bei aktivem Todes-Signal nur den
    personalisierten Begruendungszusatz. Sonst kommt die defensive Option als
    ZUSAETZLICHER, markierter Eintrag ans Ende (`defensive_slot: True`).

    **Warum additiv statt verdraengend** (Kontrolllauf 2026-07-31, Backtest
    16.15, 38.340 Samples auf derselben Trainings-KB wie die V2-03-/V2-05-
    Gates): Die urspruengliche Variante ersetzte den schwaechsten der drei
    Eintraege. Das kostete Hit@3 zwar nur 0,03 pp (68,43 -> 68,40 %, Boots
    exakt unveraendert) - aber die Vorgabe fuer diese Schicht war "hit@3 darf
    NICHT fallen", und ein verdraengter Eintrag ist gemessene Evidenz, die
    verschwindet. Additiv kann Hit@3 strukturell nicht sinken: die gemessene
    Kandidaten-Top-3 (`replay_profile.replay_candidates`) bleibt Zeichen fuer
    Zeichen dieselbe, der Zusatz-Eintrag steht dahinter. Der Preis ist ein
    zusaetzlicher Eintrag im Block, wenn der Layer feuert - und genau der ist
    der Punkt: der Spieler sieht dann nicht mehr NUR Glass-Cannon-Items."""
    if not _defensive_layer_active(ctx):
        return chosen
    already = next((r for r in chosen if r.get("defensive")), None)
    if already is not None:
        note = _death_note(ctx, already["item"])
        if note:
            already["reason"] = already["reason"].rstrip() + " " + note.rstrip()
        return chosen
    taken = {r["item"] for r in recs} | {r["item"] for r in chosen}
    rec = _defensive_rec(ctx, taken)
    return chosen if rec is None else chosen + [rec]


# Wie viele situative Kandidaten angezeigt werden. Die Liste ist seit der
# Pool-Sortierung (s. `_display_order`) eine gewichtete Scan-Liste: der Spieler
# sucht darin seinen Gedanken ("etwas gegen AD", "Damage gegen Tanks"), also
# darf sie laenger sein als die gemessene Top-3. Hit@3 misst weiterhin nur die
# ersten drei Kandidaten - die Verlaengerung beruehrt die Metrik nicht.
SITUATIONAL_SHOWN = 6


def _score_situationals(ctx: _RecContext, recs: list[dict]) -> None:
    """Phase 6 (Befund S1): der grosse Scoring-Loop ueber situational_source +
    Klassen-Kandidaten, Sortierung und Auswahl der `SITUATIONAL_SHOWN` besten.
    Haengt die gewaehlten situativen Items an recs."""
    weights = ctx.weights
    split, top, stance = ctx.split, ctx.top, ctx.stance
    threat_items, threat_base = ctx.threat_items, ctx.threat_base
    enemy_bucket, bt = ctx.enemy_bucket, ctx.bt
    state_items, state_base = ctx.state_items, ctx.state_base
    gold_state, owned_names, owned_ids = ctx.gold_state, ctx.owned_names, ctx.owned_ids
    class_label, lookup_role, class_games = ctx.class_label, ctx.lookup_role, ctx.class_games
    partner_on = _partner_layer_active(ctx)
    tag_role = _tag_role(ctx)
    # next_after-Bigramm (T2): bedingte Verteilung + Marginal-Referenz sind schon
    # in _build_context gebaut (seit V2-05 teilt sich der Core-Pick dasselbe
    # Modell); `na_owned` sind die FERTIGEN Items im Besitz (Menge O).
    na_cond, na_marginal, na_owned = ctx.na_cond, ctx.na_marginal, ctx.na_owned
    # Restpfad-Neubewertung (V2-05): Slot-Support daempft den Basisterm, Items
    # ohne Support im aktuellen Slot landen auf `path_block`, die Pool-Scores
    # wandern nach ctx (Entscheidung faellt in _assemble_result).
    path_on = weights.path_rescore_factor > 0.0
    path_scores: dict[str, float] = {}
    path_block: set[str] = set()

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
        if (name in owned_names or items.conflicts(name, owned_names)
                or _learned_conflict(ctx, name)):
            continue
        slot_mult = 1.0
        if path_on:
            slot_mult, now_ok, later_ok = _slot_support(
                ctx, name, entry.get("avg_slot"))
            if not later_ok:
                # Default (`slot_late_keep=False`): Slot-Daten enden vor dem
                # aktuellen Slot -> kein Kandidat mehr.
                continue
            if not now_ok:
                # Support erst spaeter ODER nur frueher (Datenende, s.
                # `_slot_support`): bleibt im Pool (Sichtbarkeit), darf aber
                # nicht als naechster Kauf vorgeschlagen werden.
                path_block.add(name)
        # Basis-Score = Pick-Rate, multiplikativ mit dem next_after-Lift. Der
        # Lift greift BEWUSST am Basisterm an, nicht am Endscore: der Endscore
        # kann durch Redundanz-/Partner-Abzuege negativ werden, und ein Faktor
        # > 1 wuerde ein negatives Ergebnis noch weiter nach unten schieben -
        # also genau in die falsche Richtung wirken. Der Basisterm ist immer
        # >= 0, damit ist die Richtung des Lifts eindeutig. Ausgeschlossene
        # Kandidaten (owned/conflicts) sind oben bereits raus - sie bekommen
        # nie einen Lift.
        score = entry["pick_rate"] * slot_mult * _next_after_lift(
            na_cond, na_marginal, na_owned, name, weights.next_after_factor)
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
        # next_after (T3): der Lift oben hat das Item angehoben - das gehoert in
        # die Begruendung, sonst steht im Text eine Reihenfolge, die der Nutzer
        # nicht nachvollziehen kann. Genannt wird der GRUND (der Uebergang aus
        # dem eigenen bisherigen Build), nicht das Ergebnis.
        if na_cond:
            hit = _next_after_reason(na_cond, na_marginal, na_owned, name)
            if hit:
                prev, share, n_na, ratio = hit
                # Der nackte Anteil traegt die Begruendung nicht ("18 %" klingt
                # nach wenig): erst das Verhaeltnis zum Normalfall zeigt, WARUM
                # das Item hier weiter oben steht.
                extra += (f" - folgt in {share:.0%} der Spiele direkt auf dein "
                          f"{prev}, {ratio:.1f}x so oft wie sonst (n={n_na})")
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
        # Die Stance verschiebt hier NICHTS mehr am Score (Befund D, Pfad beim
        # Testsuite-Review 2026-08-04 entfernt). Geblieben ist der Anzeige-Teil:
        # bei defensiver Lage nennt die Begruendung eines defensiven Items den
        # groessten Bedroher, damit der Text die Lage aufgreift.
        if defensive and stance == "defensive":
            threat = f" - Top-Threat: {top['name']} ({top['build_profile']})" if top else ""
            why = f"{purpose}{threat} ({stats})." if purpose else why
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
        rec = {"item": name, "kind": "situational", **tag_fields(name, role=tag_role),
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
            # Nur Champion-Evidenz kommt in den Pool: Klassen-Kandidaten ranken
            # per Design IMMER hinter den eigenen (rein additiv) und duerfen
            # darum auch den `next`-Kandidaten nicht bestimmen.
            path_scores[name] = score
    scored.sort(key=lambda row: row[0], reverse=True)
    class_scored.sort(key=lambda row: row[0], reverse=True)
    # Klassen-Kandidaten IMMER hinter den eigenen ranken (additiv): so kann der
    # Fallback nur leere Plaetze fuellen, aber keinen Champion-Kandidaten aus den
    # angezeigten Plaetzen verdraengen (Hit@3 kann dadurch nicht sinken).
    if path_on:
        # NEU zuweisen statt mutieren: _second_next_pick arbeitet auf einer
        # dataclasses.replace-Kopie, die sich die Dict-Referenzen mit dem
        # Original teilt (gleiche Regel wie in _conditional_layers). Muss VOR
        # der Slot-Reservierung passieren: die fragt ueber `_path_winner` genau
        # diese Pool-Scores ab, um den `next`-Kandidaten nicht zu verdraengen.
        ctx.path_scores = {**ctx.path_scores, **path_scores}
        ctx.path_block = ctx.path_block | frozenset(path_block)
    # Behind-Situationals (V2-08): bei defensiver Stance oder Todes-Signal
    # bekommt der Block mindestens eine defensive Option.
    recs.extend(_reserve_defensive(
        ctx, recs, [rec for _, rec in (scored + class_scored)[:SITUATIONAL_SHOWN]]))


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
    # (2) Questkette weit genug? Unter der Anzeige-Regel "nur plausible
    # naechste Kaeufe" (plan_next_item_only.md §4.3) zaehlt NUR die letzte Stufe
    # (Bounty of Worlds): davor liegen noch Quest-Stufen zwischen dem Spieler und
    # der Endform, die Karte waere also eine Empfehlung fuer "irgendwann
    # spaeter". Ohne die Regel (Ablation) gilt wie bisher jedes offene
    # Questketten-Item.
    quest_ids = ({items.SUPPORT_QUEST_FINAL_ID} if ctx.weights.next_only_display
                 else items.SUPPORT_QUEST_IDS)
    if not (set(ctx.owned_ids) & quest_ids):
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
    # `support_final`: Herkunfts-Flag fuer `_display_order` (Gruppe 1). Der Name
    # allein reicht dort NICHT als Kennzeichen - die fuenf Endformen stehen auch
    # als regulaere Core-/Situational-Kandidaten mit Pick-Rate in der KB.
    out = [{"item": primary, "kind": "core",
            **tag_fields(primary, role=tag_role),
            "reason": reason, "avg_slot": None, "support_final": True}]
    # Defensiver Zweitvorschlag: Celestial Opposition (Schild), wenn die Lage
    # defensiv ist und es nicht ohnehin schon der Primaervorschlag ist.
    celestial = items.CELESTIAL_OPPOSITION
    if primary != celestial and (ctx.stance == "defensive" or ctx.gold_state == "behind"):
        out.append({"item": celestial, "kind": "situational",
                    **tag_fields(celestial, role=tag_role),
                    "reason": ("Defensive Alternative: Schild-Item "
                               "bei Rueckstand/Unter-Druck."),
                    "defensive": True, "avg_slot": None,
                    "support_final": True})
    return out
