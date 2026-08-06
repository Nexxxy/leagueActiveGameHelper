"""next_after-Bigramm-Lift: Modell, Lift und Reason-Text des Uebergangs-Signals.

Aus recommend.py ausgelagert (Modul-Split, T2).
"""

from . import items
from .rec_context import _shrunk
from .rec_weights import RANK_MIN_N

# --- next_after-Bigramm-Lift (T2) ------------------------------------------
# Idee (plan_roadmap.md, Validierung 2026-07-29): Was ein Spieler als naechstes
# fertiges Item baut, haengt stark davon ab, was er schon FERTIG hat - staerker,
# als das marginale Aggregat hergibt (Kraken -> D&D 66 % vs. Trinity -> Spear
# 85 %). Statt eines gemeinsamen Slices (der auf einstellige Samples kollabiert)
# wird das Signal faktorisiert und MULTIPLIKATIV verrechnet:
#
#   lift_o(C) = P(C | o) / P(C)
#     P(C | o) = Anteil von C unter den Nachfolgern des besessenen Items o
#                (count-basiert, aus dem next_after-Block).
#     P(C)     = Marginal-Referenz: Anteil von C an ALLEN Uebergaengen des
#                Champion+Rollen-Blocks. Zaehler und Nenner stammen damit aus
#                derselben (gepruneten) Datenquelle - der Quotient ist
#                selbstkonsistent und braucht kein zweites Aggregat.
#
# Jeder Einzel-Lift wird mit der bestehenden K-Shrinkage Richtung 1.0 (neutral)
# gezogen, damit duenne Uebergaenge nichts verschieben. Mehrere besessene Items
# -> arithmetisches Mittel der verfuegbaren Lifts. Kein Datum -> exakt 1.0
# (automatischer Backoff).


def _next_after_model(block: dict) -> tuple[dict, dict]:
    """(bedingt, marginal) aus einem next_after-Block.

    bedingt:  {<Vorgaenger>: {<Nachfolger>: (P(C|o), count)}}
    marginal: {<Nachfolger>: P(C)} ueber alle Uebergaenge des Blocks."""
    cond: dict[str, dict[str, tuple[float, int]]] = {}
    totals: dict[str, int] = {}
    grand = 0
    for prev, succs in (block or {}).items():
        total = sum(s.get("count", 0) for s in succs)
        if total <= 0:
            continue
        cond[prev] = {s["item"]: (s.get("count", 0) / total, s.get("count", 0))
                      for s in succs}
        for s in succs:
            c = s.get("count", 0)
            totals[s["item"]] = totals.get(s["item"], 0) + c
            grand += c
    marginal = {n: c / grand for n, c in totals.items()} if grand else {}
    return cond, marginal


def _owned_completed(owned_ids: list[int]) -> list[str]:
    """Namen der FERTIGEN Items im Besitz (Menge O des Lifts) - dieselbe
    completed-Definition wie auf der Pipeline-Seite (items.is_completed)."""
    out = []
    for iid in owned_ids or []:
        if items.is_completed(iid):
            name = items.name_of(iid)
            if name:
                out.append(name)
    return out


def _next_after_lift(cond: dict, marginal: dict, owned: list[str],
                     name: str, factor: float) -> float:
    """Multiplikativer Lift fuer Kandidat `name` (1.0 = neutral).

    `factor` daempft den fertigen Lift linear Richtung 1.0 (0.0 = Schicht aus,
    1.0 = voller Lift). Live laeuft der halbe Lift (0.5) - Ergebnis des
    T3-Backtest-Gates, siehe Weights.next_after_factor."""
    if factor <= 0.0 or not cond:
        return 1.0
    p_c = marginal.get(name)
    if not p_c:
        return 1.0
    lifts = []
    for o in owned:
        hit = cond.get(o, {}).get(name)
        if hit is None:
            # Kein Uebergang o -> C in den Daten (nie beobachtet ODER unter der
            # Prune-Schwelle): kein Datum, also KEIN Beitrag - statt eines
            # stillen Malus aus einer Datenluecke.
            continue
        share, n = hit
        lifts.append(_shrunk(share / p_c, n, 1.0))
    if not lifts:
        return 1.0
    return 1.0 + factor * (sum(lifts) / len(lifts) - 1.0)


# Ab welchem Verhaeltnis P(C|o) / P(C) der Uebergang ERWAEHNT wird. Der Lift
# selbst wirkt schon darunter (fein dosiert ueber die Shrinkage) - aber ein Satz
# im Reason-Text braucht einen Befund, der die Aufmerksamkeit auch wert ist:
# 1.25 = das Item folgt mindestens ein Viertel haeufiger auf den bisherigen Build
# als im Schnitt. Darunter waere der Satz eine Nullaussage.
NEXT_AFTER_NOTE_MIN_RATIO = 1.25


def _next_after_reason(cond: dict, marginal: dict, owned: list[str],
                       name: str) -> tuple[str, float, int, float] | None:
    """(Vorgaenger, Anteil, n, Verhaeltnis zum Schnitt) des Uebergangs, der den
    Lift von `name` nach OBEN treibt - oder None. Genau die Zellen, die auch der
    Text ausweisen darf:

    - nur ANHEBENDE Uebergaenge ab NEXT_AFTER_NOTE_MIN_RATIO: ein Text zu einem
      daempfenden Lift wuerde eine Abwertung mit einer hohen Prozentzahl
      begruenden und damit das Gegenteil des Gemeinten sagen.
    - nur Zellen ab RANK_MIN_N - dieselbe Ehrlichkeitsschwelle wie bei
      by_threat/by_state: keine Prozentzahlen aus einer Handvoll Spiele.
    - der staerkste Uebergang gewinnt (Mehrfachbesitz nennt EINEN Grund, nicht
      eine Aufzaehlung)."""
    p_c = marginal.get(name)
    if not p_c:
        return None
    best = None
    for o in owned:
        hit = cond.get(o, {}).get(name)
        if hit is None:
            continue
        share, n = hit
        ratio = share / p_c
        if n < RANK_MIN_N or ratio < NEXT_AFTER_NOTE_MIN_RATIO:
            continue
        if best is None or ratio > best[3]:
            best = (o, share, n, ratio)
    return best
