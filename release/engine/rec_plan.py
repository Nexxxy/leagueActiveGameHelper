"""Kauf-Auswahl und Kaufplan-Leiste: der eine konkrete naechste Kauf
(`_pick_next` inkl. Elixier-Endstufe) und die flache Schritt-Liste darunter
(Feature 001) samt zweitem grossem Item.

Aus recommend.py ausgelagert (Modul-Split, T6).
"""

from dataclasses import replace

from . import items, knowledge
from .rec_boots import _boots_class, _boots_kb
from .rec_context import _RecContext, _tag_role
from .rec_display import _boots_gate_open, _completed_legendaries, _path_winner
from .rec_explain import _item_tag, tag_fields
from .rec_next_after import _owned_completed
from .rec_path import _core_pick, _current_slot
from .rec_situational import (
    _conditional_layers, _score_situationals, _support_final,
)

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
               owned_ids: list[int],
               role: str | None = None, slot_role: str | None = None,
               next_block: frozenset | set | None = None,
               path_first: str | None = None) -> dict | None:
    """Waehlt aus den Empfehlungen den einen konkreten naechsten Kauf.

    Reihenfolge: die aus den Timelines gelernte Kaufposition (avg_slot)
    entscheidet zwischen Core und Boots - ohne Timeline-Daten gilt die
    Faustregel: Boots vor dem zweiten fertigen Item.
    Solange NOCH KEIN fertiges Item im Inventar ist, gilt davor ein hartes Gate
    (s. `boots_first`): fertige Boots nur, wenn die gelernte `slot_dist` sie als
    Erstkauf ausweist und das gelernte Kauf-Timing erreicht ist.
    Ist das Inventar voll (6 regulaere Slots), wird ein Item-Tausch
    vorgeschlagen statt eines unkaufbaren siebten Items.

    Die `stance` steuert hier NUR noch die aggressive Abweichung (Damage-Spike
    vor Boots, wenn man vorne liegt) - sie kommt aus der ANZEIGE-Stance. Der
    frueher davorliegende Defensiv-Sonderpfad ("ein defensiver Kauf sichert ab")
    ist mit der Stance-Score-Schicht entfallen (Befund D, 2026-07-13; der
    Backtest hat ihn widerlegt).

    Restpfad-Modus (V2-05, beide Parameter leer = altes Verhalten):
    `next_block` sind Namen ohne Slot-Support im aktuellen Slot - sie duerfen
    nicht `next` werden (bleiben aber in der Liste sichtbar); `path_first` ist
    der Sieger des gemeinsamen Kandidatenpools und tritt an die Stelle, die
    vorher das Core-Item per Vorfahrt hatte.
    """
    blocked = next_block or frozenset()
    if not recs:
        return _elixir_next(owned_ids, current_gold, slot_role=slot_role)
    completed_owned = _completed_legendaries(owned_names)

    def core_why(pick: dict) -> str:
        if pick["kind"] != "core":
            # Restpfad-Modus: der Pool-Sieger ist ein Situational - dann waere
            # "Vervollstaendigt deinen Core" schlicht falsch.
            return f"Bester Kandidat fuer deinen {completed_owned + 1}. Kauf: "
        if completed_owned == 0:
            return "Erstes fertiges Item - dein wichtigster Power-Spike: "
        return f"Vervollstaendigt deinen Core als {completed_owned + 1}. fertiges Item: "

    boots = next((r for r in recs if r["kind"] == "boots"), None)

    def boots_first(other: dict) -> bool:
        """Kommen Boots vor `other`? Vor dem ersten fertigen Legendary gilt das
        harte Gate (`_boots_gate_open`: fertige Boots als ALLERERSTER Kauf sind
        fuer die meisten Champions datenwidrig, und der avg_slot-Vergleich
        allein liess sie trotzdem durch - Boots avg_slot 2,1 <= Core 2,5 heisst
        "Boots eher vor dem Core-Item", nicht "Boots zuerst"), danach
        entscheidet avg_slot bzw. die Faustregel."""
        if not _boots_gate_open(boots, completed_owned, game_time):
            return False
        if completed_owned == 0:
            # Gate offen bei 0 Legendaries heisst: echter Boots-first-Champion
            # UND Kauf-Timing erreicht - genau dann kommen sie zuerst.
            return True
        if boots.get("avg_slot") and other.get("avg_slot"):
            return boots["avg_slot"] <= other["avg_slot"]
        # Ohne gelernte Reihenfolge: bei Lead den Damage-Spike vor die Boots
        # ziehen (aggressive Deviation), sonst die Faustregel "Boots vor dem
        # zweiten fertigen Item" - die ab hier immer erfuellt ist.
        return stance != "aggressive"

    pick = None
    why_now = ""
    # Anti-Heal wird NICHT als naechster Kauf erzwungen (nur als situatives Item
    # + Alert sichtbar) - der Core-Build hat Vorrang. Es kann hoechstens ueber
    # die Standard-Situational-Auswahl drankommen.
    # Hauptkandidat: im Restpfad-Modus der Pool-Sieger (Core-Status ist dort
    # nur noch ein Prior-Bonus, keine Vorfahrt), sonst das Core-Item.
    core = None
    if path_first:
        core = next((r for r in recs if r["item"] == path_first
                     and r["item"] not in blocked), None)
    if core is None:
        core = next((r for r in recs if r["kind"] == "core"
                     and r["item"] not in blocked), None)
    if core and boots:
        if boots_first(core):
            pick, why_now = boots, ("Laut High-Elo-Kaufreihenfolge sind "
                                    "jetzt erst Boots dran: ")
        elif stance == "aggressive" and not core.get("avg_slot"):
            pick, why_now = core, "Du bist vorne - Damage-Spike vor Boots: "
        else:
            pick, why_now = core, core_why(core)
    elif core:
        pick, why_now = core, core_why(core)
    elif boots:
        pick, why_now = boots, "Dir fehlen noch Boots: "
    if pick is None:
        # Letzter Ausweg: irgendetwas muss empfohlen werden. Hat JEDER Kandidat
        # im aktuellen Slot keinen Support, gewinnt der bestplatzierte trotzdem -
        # eine leere Empfehlung waere fuer den Spieler wertloser als eine
        # zeitlich untypische (und wuerde die Backtest-Stichprobe verzerren).
        pick = next((r for r in recs if r["item"] not in blocked), recs[0])
        why_now = "Core und Boots sind komplett - staerkstes situatives Item: "

    entry = items.by_name().get(pick["item"])
    cost = entry[1].get("gold", {}).get("total", 0) if entry else 0
    # Teilitems im Inventar (z.B. Sheen fuer Trinity Force) senken den Kaufpreis
    cost_remaining = items.remaining_cost(pick["item"], owned_ids)
    # `tag` darf der Aufrufer ueberschreiben (z.B. "Anti-Heal" aus der kuratierten
    # Schicht); die beiden Anzeige-Achsen kommen IMMER frisch aus dem Item-Namen -
    # sie sind reine Item-Eigenschaften, kein kuratierter Text.
    result = {"item": pick["item"], "kind": pick["kind"],
              **tag_fields(pick["item"], role=role),
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
    has_boots2 = any(items.is_upgraded_boots(n) for n in owned2_names)
    ctx2 = replace(ctx, owned_names=owned2_names, owned_ids=owned2_ids,
                   has_boots=has_boots2,
                   # Restpfad-Modus (V2-05): der Slot rueckt mit dem angenommenen
                   # Kauf weiter, die Besitzmenge des next_after-Lifts auch.
                   cur_slot=_current_slot(owned2_ids, has_boots2),
                   slot_horizon=ctx.slot_horizon,
                   na_owned=(_owned_completed(owned2_ids) if ctx.na_cond else []),
                   path_scores={}, path_block=frozenset())
    recs2: list[dict] = []
    _core_pick(ctx2, recs2)
    recs2 += _boots_kb(ctx2)
    _conditional_layers(ctx2)
    recs2 += _boots_class(ctx2)
    _score_situationals(ctx2, recs2)
    return _pick_next(recs2, ctx2.stance, owned2_names, ctx2.game_time,
                      ctx2.current_gold, owned2_ids, role=_tag_role(ctx2),
                      slot_role=ctx2.role or ctx2.used_role,
                      next_block=ctx2.path_block,
                      path_first=_path_winner(ctx2, recs2))


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
    #    Der Endform-Ausschluss haengt an der HERKUNFT (aktive Schicht 5, hier
    #    `bool(support_name)`), nicht am NAMEN - gleicher Grundsatz wie bei
    #    `_display_order` Gruppe 1: die fuenf Endformen stehen seit der
    #    `classify_items`-Erweiterung auch als ganz normale Core-/Situational-
    #    Kandidaten MIT Pick-Rate in der Wissensbasis und werden regulaer
    #    gescort, wenn die Schicht gar nicht aktiv ist (Quest abgeschlossen oder
    #    nie getragen). Ein reiner Namens-Match warf so eine legitim gewaehlte
    #    Endform aus der Leiste, obwohl sie in `items[]`/`second_pick` steht -
    #    das Rezept-Handling traegt sie (400 G, mit `from`). Ist die Schicht
    #    AKTIV, bleibt der Ausschluss noetig: die Endform kommt dann als
    #    kostenloser Upgrade-Schritt unten dazu (sonst Dopplung), und eine
    #    ZWEITE Endform daneben waere ohnehin falsch (man waehlt genau eine).
    future: list[tuple[float | None, str, object]] = []
    if not next_is_support:
        second = second_pick
        b_name = second.get("item") if second else None
        if (b_name and second.get("kind") != "consumable" and b_name != next_name
                and not (support_name and b_name in items.COMPLETED_SUPPORT_ITEMS)):
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
