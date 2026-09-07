"""Slot-Tausch: der eigene Zustand der Empfehlung, sobald alle sechs regulaeren
Slots mit FINALEN Items belegt sind (plan_slot_tausch.md).

Drei Bausteine, in dieser Reihenfolge:

* **Gate** (`_swap_gate_open`): Getauscht wird erst, wenn es nichts mehr
  auszubauen gibt. Steht noch ein Teilitem oder ein Consumable im Slot, BAUT der
  Spieler gerade - dann ist "verkauf etwas" der falsche Rat, und `_slot_blocker`
  sagt nur noch, WORAN der Kauf haengt. Vorher gab es diese Unterscheidung
  nicht: das billigste Slot-Item wurde zum Opfer erklaert, und das war im
  Zweifel genau das Teilitem, das der Spieler gerade fertigbaut.
* **Opferwahl** (`_swap_offer`): Verkauft wird das Item mit dem geringsten Wert
  FUER DIESE SITUATION, nicht das billigste. Gemessen wird dieser Wert im
  Counterfactual - die komplette Pool-Bewertung noch einmal auf dem Inventar
  OHNE das Opfer (F2). Damit gilt fuer den Verkauf dieselbe Skala wie fuer den
  Kauf, statt einer zweiten, eigenen Wertformel daneben.
* **Sinn-Pruefung** (A4): Getauscht wird nur, wenn das Kauf-Item in genau
  diesem Counterfactual besser abschneidet als das Opfer. Sonst ist der Tausch
  ein Verlustgeschaeft (Verkaufserloes ~30 % unter Einkaufswert), und die
  Empfehlung sagt das ehrlich, statt einen Tausch um des Tauschens willen
  vorzuschlagen.

Aufgerufen wird das alles in `recommend._assemble_result` NACH `_pick_next` -
die Wahl des Kauf-Items Y aendert der Tausch nicht, er beantwortet nur die
Anschlussfrage "wofuer macht Y Platz?".
"""

from dataclasses import replace

from . import items
from .rec_boots import _boots_class, _boots_kb, _boots_pool_entry
from .rec_context import _RecContext
from .rec_next_after import _owned_completed
from .rec_path import _core_pick, _current_slot
from .rec_situational import _conditional_layers, _score_situationals

# Regulaere Item-Slots; Boots und Trinket haben eigene Slots (Details in
# `items.slot_items`). Die Konstante liegt hier, weil das Slot-Gate ihr
# eigentlicher Nutzer ist - rec_plan importiert sie von hier.
ITEM_SLOTS = 6

# Ab wieviel Gold ueberhaupt ueber einen Tausch nachgedacht wird (A6).
# WARUM eine Schwelle: der Verkauf bringt nur rund 70 % des Einkaufswerts
# zurueck. Wer knapp bei Kasse ist, steht nach dem Verkauf ohne das alte UND
# ohne das neue Item da - der Tausch ist dann kein Plan, sondern ein Loch.
# Bewusst KEIN Weights-Feld: die Schwelle ist eine Anzeige-Regel fuer den
# Live-Fall und hat im Backtest (current_gold=None) gar keinen Angriffspunkt.
SWAP_MIN_GOLD = 2000

NOTE_MIN_GOLD = f"Build komplett - ein Tausch lohnt ab {SWAP_MIN_GOLD} G"
NOTE_NO_GAIN = "Build komplett - kein Tausch, der sich lohnt"


# --- Gate und Blocker -------------------------------------------------------

def _slot_final(item_id: int, item: dict) -> bool:
    """Ist dieses Slot-Item FERTIG - also nichts, was der Spieler gerade ausbaut?

    Fertig heisst: fertiges Legendary (`items.is_completed`, schliesst die
    Support-Endformen ein) oder fertige Boots ab T2 (`items.is_upgraded_boots`).

    Das `into` der T2-Boots zaehlt BEWUSST nicht als "ausbaubar": Data Dragon
    16.17 fuehrt Plated Steelcaps -> Armored Advance und Mercury's Treads ->
    Chainlaced Crushers, aber die T3-Stufe steht nur dem Gewinner der Feats of
    Strength offen. Fuer alle anderen sind T2-Boots das Ende der Fahnenstange -
    ein Gate ueber `into` wuerde sie dauerhaft als "noch im Bau" fuehren und den
    Tausch nie freigeben."""
    tags = set(item.get("tags", []))
    if tags & {"Consumable", "Trinket"}:
        return False
    return (items.is_completed(item_id)
            or items.is_upgraded_boots(item.get("name", "")))


def _swap_gate_open(owned_ids: list[int], slot_role: str | None = None) -> bool:
    """Sind alle sechs regulaeren Slots mit finalen Items belegt?

    Nur dann ist ein Tausch ueberhaupt das Thema. `slot_role` steuert die
    rollenbewusste Slot-Zaehlung (Boots belegen ausserhalb BOTTOM einen Slot,
    der Control Ward bei UTILITY nicht)."""
    slots = items.slot_items(owned_ids, role=slot_role)
    return (len(slots) >= ITEM_SLOTS
            and all(_slot_final(iid, item) for iid, item in slots))


def _slot_blocker(owned_ids: list[int],
                  slot_role: str | None = None) -> dict | None:
    """Das Item, an dem ein Kauf haengt, wenn das Inventar voll, aber NICHT
    final ist: das erste Teilitem bzw. Consumable in Inventar-Reihenfolge
    (A1). Rueckgabe `{"item", "item_id", "kind"}` mit `kind` "component" oder
    "consumable" - der Unterschied traegt den Frontend-Text ("sobald X fertig
    ist" vs. "sobald X genutzt ist").

    None heisst: kein Blocker zu nennen - entweder ist noch ein Slot frei, oder
    alle sechs sind final (dann ist der Tausch dran, nicht der Hinweis)."""
    slots = items.slot_items(owned_ids, role=slot_role)
    if len(slots) < ITEM_SLOTS:
        return None
    for item_id, item in slots:
        if _slot_final(item_id, item):
            continue
        consumable = "Consumable" in set(item.get("tags", []))
        return {"item": item.get("name"), "item_id": item_id,
                "kind": "consumable" if consumable else "component"}
    return None


def _needs_slot(pick: dict, owned_ids: list[int]) -> bool:
    """Braucht dieser Kauf einen freien regulaeren Slot?

    Nein bei Boots (eigener Slot) und nein, wenn er ein vorhandenes Teilitem
    verschmilzt - dann belegt das Ergebnis den Slot des Teilitems weiter."""
    return (pick.get("kind") != "boots"
            and items.build_discount(pick.get("item", ""), owned_ids) == 0)


def _slot_blocked(pick: dict, owned_ids: list[int],
                  slot_role: str | None = None) -> bool:
    """Braucht der Kauf einen Slot, ohne dass noch einer frei ist?

    Der gemeinsame Test hinter zwei Dingen: dem `slot_block`-Feld auf der
    Next-Karte (A1) und dem Ende der Kaufplan-Leiste am sechsten Slot (A9)."""
    return (_needs_slot(pick, owned_ids)
            and len(items.slot_items(owned_ids, role=slot_role)) >= ITEM_SLOTS)


# --- Situationswert im Counterfactual ---------------------------------------

def _counterfactual(ctx: _RecContext, victim_id: int,
                    victim_name: str) -> tuple[dict, dict | None]:
    """Die komplette Pool-Bewertung noch einmal auf dem Inventar OHNE das Opfer
    X. Rueckgabe: (Pool-Scores, Primaer-Boots-Empfehlung oder None).

    Baut auf demselben Muster wie `_second_next_pick` auf: eine
    `dataclasses.replace`-Kopie des Kontexts, dann die Phasen-Helfer der Reihe
    nach. Die abgeleiteten Felder (`has_boots`, `cur_slot`, `na_owned`) muessen
    dabei ALLE neu gerechnet werden - ein stehen gebliebenes Feld aus dem echten
    Inventar wuerde die hypothetische Bewertung still verfaelschen. `path_scores`
    und `path_block` starten leer, damit nur die Zahlen dieses Laufs drinstehen.

    `slot_neutral=True` schaltet die Slot-Schicht ab: der "aktuelle Kaufslot"
    ist hier eine Fiktion (das Inventar hat gerade ein Loch, das der Spieler nie
    hatte), und ein Item danach zu daempfen oder auszuschliessen wuerde
    Kaufzeitpunkte bewerten statt Situationswert. Gefragt ist aber genau
    letzterer: "wie viel ist mir dieses Item JETZT wert?"."""
    owned_ids = [i for i in ctx.owned_ids if i != victim_id]
    if len(owned_ids) == len(ctx.owned_ids):
        # Nichts entfernt (ID nicht im Inventar) - dann waere der Lauf keine
        # Gegenprobe, sondern eine Wiederholung.
        owned_ids = list(ctx.owned_ids)
    owned_names = set(ctx.owned_names) - {victim_name}
    has_boots = any(items.is_upgraded_boots(n) for n in owned_names)
    ctx2 = replace(ctx, owned_names=owned_names, owned_ids=owned_ids,
                   has_boots=has_boots,
                   cur_slot=_current_slot(owned_ids, has_boots),
                   na_owned=(_owned_completed(owned_ids) if ctx.na_cond else []),
                   path_scores={}, path_block=frozenset(),
                   slot_neutral=True)
    recs: list[dict] = []
    _core_pick(ctx2, recs)
    recs += _boots_kb(ctx2)
    _conditional_layers(ctx2)
    recs += _boots_class(ctx2)
    _score_situationals(ctx2, recs)
    # Boots als Kategorie in den Pool heben - sonst haetten ausgerechnet sie als
    # einziger Kandidat keinen Score und waeren automatisch immer das Opfer.
    _boots_pool_entry(ctx2, recs)
    boots = next((r for r in recs if r.get("kind") == "boots"
                  and not r.get("alternative")), None)
    return ctx2.path_scores, boots


def _kb_pick_rate(ctx: _RecContext, name: str) -> float | None:
    """Pick-Rate eines Items in der Wissensbasis dieser Kombi - oder None, wenn
    die KB es gar nicht kennt (dann ist "wird hier praktisch nicht gebaut" die
    ehrliche Aussage, nicht "0 % Pick")."""
    for section in (ctx.core_source, ctx.situational_source, ctx.boots_options,
                    ctx.class_situational, ctx.class_boots):
        for entry in section or []:
            if entry.get("item") == name:
                return entry.get("pick_rate")
    return None


def _victim_row(ctx: _RecContext, buy_name: str, item_id: int,
                item: dict) -> dict:
    """Bewertungszeile fuer EIN moegliches Opfer: sein Situationswert, der Wert
    des Kauf-Items im selben Lauf und die Angaben, die Tie-Break (A5) und
    Begruendung (A7) brauchen."""
    name = item.get("name", "")
    scores, boots = _counterfactual(ctx, item_id, name)
    other_boots = None
    if items.is_upgraded_boots(name):
        # F3: Boots sind ein Kandidat wie jeder andere - aber ihr Wert haengt
        # daran, ob die Boots-Schicht sie im Counterfactual ERNEUT waehlen
        # wuerde. Faellt die Wahl auf andere Boots, sind es die falschen Boots
        # fuer diese Gegner-Comp und damit das erste Opfer.
        if boots is None or boots.get("item") != name:
            value = 0.0
            other_boots = boots.get("item") if boots else None
        else:
            value = scores.get(name)
            if value is None:
                # Ohne Merge-`slot_dist` gibt es keinen Kategorie-Score
                # (`_boots_pool_score` liefert None) - dann traegt die nackte
                # Pick-Rate der Boots den Vergleich.
                value = _kb_pick_rate(ctx, name) or 0.0
    else:
        # Ohne Pool-Score ist das Item in dieser Situation kein Kandidat mehr -
        # Wert 0, und damit das erste Opfer (F2).
        value = scores.get(name, 0.0)
    return {"item": name, "item_id": item_id, "value": value,
            "buy_value": scores.get(buy_name, 0.0),
            "pick_rate": _kb_pick_rate(ctx, name),
            "cost": item.get("gold", {}).get("total", 0),
            "sell": item.get("gold", {}).get("sell", 0),
            "other_boots": other_boots}


def _swap_reason(ctx: _RecContext, victim: dict) -> str:
    """WARUM gerade dieses Opfer (A7) - der Grund, nicht das Ergebnis. Ein
    nackter Score waere fuer den Spieler keine Begruendung, sondern eine Zahl,
    die er nicht nachpruefen kann."""
    if victim["other_boots"]:
        return f"die Boots-Wahl fiele jetzt auf {victim['other_boots']}"
    if victim["value"] <= 0.0:
        where = " ".join(x for x in (ctx.champion, ctx.used_role or ctx.role)
                         if x)
        return f"wird auf {where} praktisch nicht gebaut"
    if victim["pick_rate"] is None:
        return "situativ dein schwaechstes Item"
    return (f"situativ dein schwaechstes Item ({victim['pick_rate']:.0%} Pick "
            f"in dieser Rolle)")


def _swap_offer(ctx: _RecContext,
                buy: dict | None) -> tuple[dict | None, str | None]:
    """Das Tausch-Angebot zum Kauf-Item `buy` - oder der Grund, warum es keins
    gibt.

    Rueckgabe `(angebot, notiz)`:
    * `({"sell_item", "sell_item_id", "sell_value", "reason"}, None)` - tauschen.
    * `(None, <Text>)` - Gate offen, aber kein Tausch: das Next-Item entfaellt,
      die Notiz tritt an seine Stelle (A2).
    * `(None, None)` - der Tausch ist gar nicht das Thema (Gate zu, Kauf
      braucht keinen Slot, kein Kauf-Item). Dann bleibt alles wie bisher.

    Die Gold-Schwelle wird bei UNBEKANNTEM Gold uebersprungen (A3): sie ist eine
    Anzeige-Regel fuer den Live-Fall. Backtest und Post-Game-Replay rufen mit
    `current_gold=None` und wuerden sonst genau die Kaeufe bei finalem Inventar
    aus der Messung verlieren."""
    if not buy or not buy.get("item"):
        return None, None
    slot_role = ctx.role or ctx.used_role
    if not _swap_gate_open(ctx.owned_ids, slot_role):
        return None, None
    if not _needs_slot(buy, ctx.owned_ids):
        # Boots (eigener Slot) oder ein Kauf, der ein Teilitem verschmilzt: der
        # passt auch ins volle Inventar, ganz ohne Verkauf.
        return None, None
    if ctx.current_gold is not None and ctx.current_gold < SWAP_MIN_GOLD:
        return None, NOTE_MIN_GOLD
    rows = [_victim_row(ctx, buy["item"], iid, item)
            for iid, item in items.slot_items(ctx.owned_ids, role=slot_role)]
    if not rows:
        return None, NOTE_NO_GAIN
    # Opfer = geringster Situationswert. Tie-Break (A5): niedrigste Pick-Rate,
    # zuletzt der niedrigste Einkaufswert - erst wenn die Situation nichts mehr
    # unterscheidet, entscheidet, was am wenigsten Gold im Boden liegen hat.
    victim = min(rows, key=lambda r: (r["value"], r["pick_rate"] or 0.0,
                                      r["cost"]))
    if victim["buy_value"] <= victim["value"]:
        # Sinn-Pruefung (A4): im selben Lauf ist das Kauf-Item nicht besser als
        # das schwaechste vorhandene. Dann ist Stehenlassen die bessere Wahl.
        return None, NOTE_NO_GAIN
    return {"sell_item": victim["item"], "sell_item_id": victim["item_id"],
            "sell_value": victim["sell"],
            "reason": _swap_reason(ctx, victim)}, None
