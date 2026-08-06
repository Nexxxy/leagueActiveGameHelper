"""Restpfad-Neubewertung (V2-05): Kaufslot, Slot-Support, gelernte Exklusivitaet
und die Core-Pick-Auswahl in beiden Modi.

Aus recommend.py ausgelagert (Modul-Split, T2).
"""

from . import items
from .rec_context import _RecContext, _tag_role
from .rec_explain import explain_item, tag_fields
from .rec_next_after import _next_after_lift

# --- Restpfad-Neubewertung (V2-05, plan_engine_v2.md Konzept 2) -------------
# Vorher war die Core-Reihenfolge statisch (avg_slot): wer The Collector
# uebersprungen hat, bekam ihn bis Spielende als naechsten Kauf empfohlen, und
# jeder tatsaechliche Kauf zaehlte als Abweichung. Jetzt wird der Restpfad nach
# jedem fertigen Item aus dem TATSAECHLICHEN Besitz neu bewertet:
#
#   Pool = restliche Core-Items + Situationals (ein Topf)
#   Score = Pick-Rate * Slot-Support(aktueller Slot) * next_after-Lift
#           + konditionale Schichten (+ Prior-Bonus fuer Core-Status)
#
# Der Slot-Support P(Slot|Item) aus V2-04 ist dabei der Troll-Guard in beide
# Richtungen: ein Item, das in KEINEM Slot ab dem aktuellen gebaut wird, ist kein
# Kandidat mehr (The Collector als 6. Item); eines mit Support erst spaeter bleibt
# im Pool, wird aber nicht als `next` vorgeschlagen (Rabadon frueh).


def _current_slot(owned_ids: list[int] | None, has_boots: bool) -> int:
    """Kaufslot des NAECHSTEN fertigen Kaufs (1-basiert).

    Spiegelt die Train-Definition aus pipeline/aggregate.py: Slot 1 ist der erste
    fertige Kauf, und BOOTS ZAEHLEN MIT (ein spaeteres T3-Upgrade desselben
    Boots-Strangs aber nicht - darum hoechstens +1 fuer Boots)."""
    return items.count_completed(owned_ids or []) + (1 if has_boots else 0) + 1


def _int_slot_dist(dist: object) -> dict[int, float]:
    """`{Kaufslot: Anteil}` mit int-Keys - defensiv aus einer rohen Verteilung.

    Handgeschriebene Overrides und YAML-Runden koennen die Slot-Keys als String
    liefern; unbrauchbare Zellen fallen still weg, statt die Leseseite an einer
    kaputten Zahl sterben zu lassen. Nicht-Dicts (None, Liste) ergeben leer."""
    if not isinstance(dist, dict):
        return {}
    clean: dict[int, float] = {}
    for slot, share in dist.items():
        try:
            clean[int(slot)] = float(share)
        except (TypeError, ValueError):
            continue
    return clean


def _slot_horizon(slot_dist: dict) -> int:
    """Hoechster Slot, fuer den die KB dieser Kombi UEBERHAUPT Support ausweist.

    Die Slot-Verteilung ist bei n < SLOT_DIST_MIN_N gepruned - je spaeter der
    Slot, desto haeufiger faellt er komplett weg. Fuer einen Support, dessen
    Items alle bei Slot 4 enden, ist "kein Support ab Slot 5" darum KEINE Aussage
    ueber den Build, sondern das Ende der Datenreichweite. Jenseits dieses
    Horizonts schaltet der Slot-Layer auf neutral (s. `_slot_support`), statt
    reihenweise Kandidaten aus einer Datenluecke heraus zu streichen."""
    return max((max(d) for d in slot_dist.values() if d), default=0)


# Wie weit DARF der aktuelle Slot ueber dem letzten belegten Slot eines Items
# liegen, bevor es als Kandidat ausscheidet? 1, weil `slot_dist` bei
# SLOT_DIST_MIN_N = 5 Beobachtungen prunet: der letzte ausgewiesene Slot ist der
# letzte, an dem GENUEGEND Leute gekauft haben, nicht der letzte, an dem
# ueberhaupt jemand gekauft hat. Ohne diese eine Slot Toleranz strich die Schicht
# reihenweise reale Kaeufe (Lulus Mikael's Blessing endet bei Slot 3, wird real
# aber auch als 4. Item gekauft - Hit@3 fiel dort um 24 Punkte).
SLOT_LATE_TOLERANCE = 1

# Gewichtung eines Items, das in einer Kombi MIT Slot-Daten selbst weder
# `slot_dist` noch `avg_slot` hat - die Timelines haben es also nie als KAUF
# gesehen. Der reale Fall dahinter sind Transform-Items: Muramana entsteht aus
# Manamune per ITEM_TRANSFORM, ein ITEM_PURCHASED-Event gibt es nie. Aus den
# End-Builds hat Jayce es als drittmeistgebautes Item (also im Core), ohne einen
# einzigen beobachteten Kaufzeitpunkt. Ungedaempft gewann es damit den Pool und
# wurde zum Dauer-Vorschlag, den der Spieler so gar nicht kaufen kann (Jayce TOP
# Hit@1 20,5 % -> 1,3 %). Halbes Gewicht plus Sperre als `next`: sichtbar bleiben
# darf es, empfohlen wird es nicht.
SLOT_NO_DATA_FIT = 0.5


def _slot_support(ctx: _RecContext, name: str,
                  avg_slot: float | None = None) -> tuple[float, bool, bool]:
    """(Slot-Faktor, Support jetzt?, Support jetzt oder spaeter?).

    Der Faktor daempft den Basisterm eines Items, das in diesem Slot untypisch
    ist: `now / peak` (Anteil im aktuellen Slot, relativ zum Lieblingsslot des
    Items), linear ueber `path_rescore_factor` Richtung 1.0 gezogen.

    Fehlt die Verteilung, greift `avg_slot` als groebere Rueckfallebene (Abstand
    zum gelernten Durchschnittsslot), und fehlt auch die, SLOT_NO_DATA_FIT. Das
    ist wichtig gegen eine stille Schieflage: ein Item OHNE `slot_dist` bliebe
    sonst ungedaempft und wuerde genau dadurch nach vorne rutschen - auf Lulu
    verdraengte der Support-Endform-Eintrag (kein slot_dist) so den Erstkauf
    Ardent Censer aus der Liste. Aus dem POOL geworfen wird ueber diese
    Rueckfallebenen NIE - `avg_slot` ist ein Mittelwert, kein Support-Nachweis.

    Bewusst NEUTRAL (Faktor 1.0, Support ja) bleibt der Fall, dass der aktuelle
    Slot jenseits des Datenhorizonts der Kombi liegt (`slot_horizon`): dort kappt
    die Pruning-Schwelle der Pipeline jede Aussage, und ein fehlendes Datum darf
    nie ein Item ausschliessen (auch alte KBs ohne das Feld landen hier).

    Der dritte Rueckgabewert ("Support jetzt oder spaeter?") ist im Default
    False, sobald der aktuelle Slot hinter dem Datenende des Items liegt - der
    Aufrufer wirft das Item dann aus dem Pool. Begruendung unten an der
    Stelle."""
    if ctx.cur_slot > ctx.slot_horizon:
        return 1.0, True, True
    f = ctx.weights.path_rescore_factor
    dist = ctx.slot_dist.get(name)
    if not dist:
        if avg_slot is None:
            # Kein einziger beobachteter Kaufzeitpunkt (s. SLOT_NO_DATA_FIT).
            return 1.0 + f * (SLOT_NO_DATA_FIT - 1.0), False, True
        fit = 1.0 / (1.0 + abs(avg_slot - ctx.cur_slot))
        return 1.0 + f * (fit - 1.0), True, True
    peak = max(dist.values()) or 1.0
    now = dist.get(ctx.cur_slot, 0.0)
    mult = 1.0 + f * (now / peak - 1.0)
    if ctx.cur_slot > max(dist) + SLOT_LATE_TOLERANCE:
        # Jenseits des ITEM-eigenen Datenendes. `now` ist hier per Konstruktion
        # 0, `mult` also schon die maximale Daempfung; die einzige Frage ist, ob
        # das Item noch Kandidat bleibt (`slot_late_keep`).
        #
        # Default ist RAUS (Hard-Drop), und zwar per Backtest entschieden. Die
        # Gegenthese war gut motiviert: das fehlende Datum ist Rechtszensierung
        # und keine Evidenz "wird so spaet nie gebaut" - die Pipeline prunet
        # Slots mit n < SLOT_DIST_MIN_N, und lange Spiele sind selten, je
        # spaeter der Slot desto sicherer faellt er unter die Schwelle.
        # Realfall aus dem 10-Matches-Review: Hwei BOTTOM, 57 Minuten.
        # Shadowflame ist dort Core-Item #3 (43 % Pick, n=128), seine
        # `slot_dist` endet aber bei Slot 4, weil in Hweis ganzer Kombi nur 3
        # von 12 Items ueberhaupt Slot-6-Eintraege haben. Beim 6. Kaufslot
        # fliegt Shadowflame aus dem Pool und die Engine empfiehlt Seraph's
        # Embrace (6 % Pick).
        #
        # Gemessen hat sich das trotzdem nicht gerechnet
        # (data/backtest/16.15/report-sweep-slot_late_keep.yaml, 76 313
        # Samples, alle KB-Kombis): mit `slot_late_keep=True` faellt Hit@3
        # gesamt von 68,9 % auf 68,6 % (-0,3 pp) und ab dem 3. Kauf - dem
        # Zielfall - von 64,4 % auf 63,7 % (-0,7 pp); Hit@1 -0,1 pp, Boots
        # unveraendert. Im Per-Kombi-Gate (n >= 50) verlieren 44 Kombis mehr
        # als 1 pp (Spitze Xerath UTILITY -4,7 pp), nur 4 gewinnen mehr als
        # 1 pp. Der Hwei-Fall ist real, aber selten - die gedaempften Spaet-
        # Kandidaten verdraengen im Mittel oefter einen richtigen Vorschlag,
        # als sie einen retten. Er wird ausserdem nachgelagert abgefedert: die
        # Post-Game-Wertung zieht ihr Gedaechtnis aus dem Core der KB und den
        # frueheren Top-3-Listen (`_core_names` / `top3_seen` in
        # app/postgame/build_replay.py) und wertet so einen spaeten
        # Core-Item-Kauf weiter als konform, auch wenn die Live-Engine ihn im
        # Moment des Kaufs nicht mehr auf der Liste hatte. `slot_late_keep`
        # bleibt als Experiment-/Ablations-Schalter erhalten, damit ein
        # kuenftiger Sweep die Frage mit neuen Daten neu stellen kann.
        #
        # Die V2-05-Absicherung bleibt trotzdem stehen: now_ok=False setzt den
        # Namen beim Aufrufer auf `path_block`, damit kann das Item sichtbar
        # bleiben, aber nie `next` werden. Genau daran haengt der Jhin/Collector-
        # Fall ("uebersprungenes Core-Item wird nicht stur weiterempfohlen") -
        # der wird vom Block getragen, nicht vom Rauswurf.
        return mult, False, ctx.weights.slot_late_keep
    return mult, now > 0, True


def _learned_conflict(ctx: _RecContext, name: str) -> bool:
    """True, wenn ein gelerntes Exklusiv-Paar (V2-04) den Kandidaten gegen ein
    besessenes Item ausschliesst. Die Paare sind Mengen - beide Richtungen sind
    damit automatisch abgedeckt."""
    return any(name in pair and (pair - {name}) & ctx.owned_names
               for pair in ctx.exclusive)


def _core_reason(ctx: _RecContext, core: dict) -> str:
    """Begruendungstext eines Core-Vorschlags (in beiden Modi identisch)."""
    reason = f"Core-Build fuer {ctx.champion} {ctx.used_role}"
    if ctx.build_reason:
        reason += f" - {ctx.build_reason}"
    purpose = explain_item(core["item"], ctx.split, ctx.top, role=_tag_role(ctx))
    if purpose:
        reason += f" - {purpose}"
    reason += f" ({core['pick_rate']:.0%} Pick-Rate in High-Elo"
    if core.get("avg_slot"):
        reason += f", typisch {core['avg_slot']:.0f}. Kauf"
    return reason + ")."


def _core_rec(ctx: _RecContext, core: dict) -> dict:
    return {"item": core["item"], "kind": "core",
            **tag_fields(core["item"], role=_tag_role(ctx)),
            "reason": _core_reason(ctx, core),
            "avg_slot": core.get("avg_slot")}


def _core_pick(ctx: _RecContext, recs: list[dict]) -> None:
    """Phase 2 (Befund S1): naechstes Core-Item, das noch fehlt.

    Legacy-Modus (path_rescore_factor == 0): das erste noch fehlende Core-Item in
    gelernter Kaufreihenfolge. Restpfad-Modus (V2-05): das Core-Item mit dem
    besten Pool-Score, Items ohne Slot-Support im aktuellen Slot sind als `next`
    gesperrt. Haengt hoechstens ein Core-Item an recs."""
    if ctx.weights.path_rescore_factor > 0.0:
        _core_pick_path(ctx, recs)
        return
    for core in ctx.core_source:
        if (core["item"] not in ctx.owned_names
                and not items.conflicts(core["item"], ctx.owned_names)
                and not _learned_conflict(ctx, core["item"])):
            recs.append(_core_rec(ctx, core))
            break


def _core_pick_path(ctx: _RecContext, recs: list[dict]) -> None:
    """Core-Auswahl im Restpfad-Modus (V2-05). Fuellt zusaetzlich
    `ctx.path_scores`/`ctx.path_block` fuer die spaetere Pool-Entscheidung."""
    scores: dict[str, float] = {}
    block: set[str] = set()
    best: tuple[float, dict, bool] | None = None
    skipped: list[dict] = []
    for core in ctx.core_source:
        name = core["item"]
        if (name in ctx.owned_names or items.conflicts(name, ctx.owned_names)
                or _learned_conflict(ctx, name)):
            continue
        mult, now_ok, later_ok = _slot_support(ctx, name, core.get("avg_slot"))
        if not later_ok:
            # Default (`slot_late_keep=False`): Item raus aus dem Pool, weil
            # seine Slot-Daten vor dem aktuellen Slot enden.
            skipped.append(core)
            continue
        score = (core.get("pick_rate", 0.0) * mult
                 * _next_after_lift(ctx.na_cond, ctx.na_marginal, ctx.na_owned,
                                    name, ctx.weights.next_after_factor)
                 # Core-Status gibt nur noch einen Prior-Bonus, keine Vorfahrt.
                 + ctx.weights.path_core_bonus)
        scores[name] = score
        if not now_ok:
            block.add(name)
        if best is None or score > best[0]:
            if best is not None:
                skipped.append(best[1])
            best = (score, core, now_ok)
        else:
            skipped.append(core)
    ctx.path_scores = scores
    ctx.path_block = frozenset(block)
    if best is None:
        return
    rec = _core_rec(ctx, best[1])
    note = _degraded_core_note(best[1], skipped)
    if note:
        rec["reason"] = rec["reason"].rstrip(".") + note + "."
    recs.append(rec)


def _degraded_core_note(chosen: dict, skipped: list[dict]) -> str:
    """Hinweistext, wenn ein FRUEHERES Core-Item uebersprungen wurde: "dein Pfad
    laeuft ueber X - Y waere ueblich 2. Item gewesen". Genannt wird das
    frueheste uebersprungene Core-Item, und nur wenn es typischerweise VOR dem
    jetzt vorgeschlagenen kommt - sonst ist es keine Abweichung, sondern die
    normale Reihenfolge."""
    chosen_slot = chosen.get("avg_slot")
    if chosen_slot is None:
        return ""
    earlier = [s for s in skipped
               if s.get("avg_slot") is not None and s["avg_slot"] < chosen_slot]
    if not earlier:
        return ""
    y = min(earlier, key=lambda s: s["avg_slot"])
    return (f" - dein Pfad laeuft ueber {chosen['item']}, "
            f"{y['item']} waere ueblich {y['avg_slot']:.0f}. Item gewesen")
