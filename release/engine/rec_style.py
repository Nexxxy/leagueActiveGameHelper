"""Spielstil-Regler (plan_spielstil.md): Item-Achse, Score-Faktor, der
Klassen-Overlay-Filter und die Begruendungstexte des Nutzer-Tilts.

Der Regler ist eine bewusste NUTZER-Vorgabe ("ich bin die einzige Frontline
meines Teams"), keine Lage-Erkennung - das ist die Stance, und die ist seit
Befund D reine Anzeige (s. `rec_stance`). Beide duerfen sich widersprechen:
vorne liegen und trotzdem tanky bauen.

Die Engine kennt nur den kontinuierlichen `Weights.style_tilt` in [-1, +1]; die
fuenf Regler-Stufen liegen als `PLAYSTYLE_TILT` in `rec_weights`. Bei Tilt 0
liefert JEDE Funktion hier den neutralen Wert (Faktor exakt 1.0, leere Texte),
damit das Verhalten ohne Regler byte-identisch bleibt.

Satelliten-Modul wie die uebrigen `rec_*`: importiert NIE `recommend` (Fassade),
sondern nur nach unten (items, champions, rec_explain, rec_weights).
"""

from . import champions, items
from .rec_explain import _is_defensive_item
from .rec_weights import Weights

# Dieselbe EINE Tag-Taxonomie wie ueberall sonst (Fix 5.6): AD+AP = offensiv,
# DEF = defensiv. Bewusst NICHT `rec_explain._tag_axis` wiederverwendet - das
# ist die ANZEIGE-Achse (Badge-Farbe) und darf sich unabhaengig entwickeln.
_OFFENSE_TAGS = items.AD_TAGS | items.AP_TAGS

# Ressourcen-Tags fuer den Klassen-Overlay-Filter (F3): ein Item, dessen Wert an
# Mana haengt, ist fuer einen Fury-/Energie-Champion Unsinn.
_MANA_TAGS = {"Mana", "ManaRegen"}


def style_axis(name: str) -> float:
    """Wo liegt das Item auf der Achse "tanky (-1) ... reiner Schaden (+1)"?

    | rein defensiv (nur DEF_TAGS)                  | -1.0 |
    | Hybrid MIT Resistenz (Zhonya, Banshee, Wit's) | -0.5 |
    | Hybrid nur HP (Dusk and Dawn, Riftmaker)      |  0.0 |
    | rein offensiv (Rabadon, Shadowflame, Nashor)  | +1.0 |
    | ohne Achse (Consumables, Unbekanntes)         |  0.0 |

    HP-Hybride sind BEWUSST neutral: sie steigen und fallen nicht. Der "HP-Fokus"
    der Stufe 1 entsteht dadurch, dass die reinen Schadens-Items unter sie
    sinken - so bleibt Gwens Signature-Item Dusk and Dawn (73 % Pick) auch auf
    Stufe 5 vorn, statt dass der Regler die Datenlage umschreibt.

    Die Trennung Resistenz vs. blosse HP kommt aus `_is_defensive_item`, damit
    es im ganzen Projekt genau EINE Definition von "taugt als defensive Option"
    gibt (Liandry's & Co. fallen dort schon heute durch)."""
    tags = items.tags_of(name)
    offensive = bool(tags & _OFFENSE_TAGS)
    if not offensive:
        return -1.0 if tags & items.DEF_TAGS else 0.0
    # Offensiv-Tags liegen an: `_is_defensive_item` kann jetzt nur noch ueber
    # eine echte Resistenz (Armor/SpellBlock) True werden.
    if _is_defensive_item(name):
        return -0.5
    return 0.0 if "Health" in tags else 1.0


def style_factor(tilt: float, axis: float, weights: Weights) -> float:
    """Multiplikator auf den BASISTERM eines Pool-Scores.

    `clamp(1 + tilt * achse * style_scale, style_floor, 2 - style_floor)`.

    Warum am Basisterm und nicht am Endscore: identische Begruendung wie beim
    next_after-Lift (s. `rec_situational._score_situationals`) - der Endscore
    kann durch Redundanz-/Partner-Abzuege negativ werden, ein Faktor > 1 zoege
    ihn dann in die falsche Richtung. Der Basisterm ist immer >= 0.

    Bei Tilt 0 (oder Achse 0) exakt 1.0, also multiplikativ neutral - das ist
    die Byte-Identitaets-Garantie des Reglers."""
    if not tilt or not axis:
        return 1.0
    floor = weights.style_floor
    return max(floor, min(2.0 - floor, 1.0 + tilt * axis * weights.style_scale))


def style_label(tilt: float) -> str:
    """Kurzname der Richtung fuer die Begruendungstexte ("" bei Tilt 0)."""
    if tilt < 0:
        return "Tanky"
    return "Carry" if tilt > 0 else ""


def style_note(tilt: float, champion: str, role: str | None) -> str:
    """Der eine erklaerende Satz unter der Empfehlung - leer bei Tilt 0.

    Nach den Verdikt-Prinzipien (arch_postgame_verdikt): nennt den GRUND und die
    GRENZE, nicht das Ergebnis. Die Grenze ist wichtig, weil sie die haeufigste
    Fehlerwartung abraeumt: fuer Gwen gibt es in den Daten kein Tank-Build, und
    Stufe 1 liefert das tankigste ERREICHBARE, nicht Jak'Sho."""
    if not tilt:
        return ""
    # Rolle kann fehlen (Kombi ohne KB-Eintrag) - dann bleibt der Champion
    # allein stehen, statt ein "None" in den Text zu schreiben.
    where = " ".join(p for p in (champion, role) if p)
    if tilt < 0:
        moved = ("reine Schadens-Items zurueckgestuft, Resistenz-Items "
                 "vorgezogen")
    else:
        moved = ("Resistenz-Items zurueckgestuft, reine Schadens-Items "
                 "vorgezogen")
    return (f"Spielstil {style_label(tilt)}: {moved} - innerhalb der Daten "
            f"von {where}, bewusste Abweichung von der gelernten "
            f"High-Elo-Reihenfolge.")


def style_reason_suffix(tilt: float, factor: float) -> str:
    """Begruendungs-Zusatz fuer EINE Karte, deren Basisterm der Regler bewegt
    hat - leer bei Tilt 0 und bei Faktor 1.0 (Achse 0: das Item ist von der
    Verschiebung gar nicht betroffen, ein Zusatz waere dort schlicht falsch).

    Ohne Vorzeichen im Text waere er eine Tautologie ("Spielstil aktiv") - er
    sagt darum, in WELCHE Richtung diese Karte verschoben wurde."""
    if not tilt or factor == 1.0:
        return ""
    direction = "vorgezogen" if factor > 1.0 else "zurueckgestuft"
    return f" - Spielstil {style_label(tilt)}: {direction}"


def add_style_reason(rec: dict, tilt: float, factor: float) -> None:
    """Haengt `style_reason_suffix` an die Begruendung einer Karte (in place).
    No-Op bei Tilt 0 - gleiche Satzzeichen-Mechanik wie bei den uebrigen
    Zusaetzen (Punkt abschneiden, anhaengen, Punkt zurueck)."""
    suffix = style_reason_suffix(tilt, factor)
    if suffix:
        rec["reason"] = rec["reason"].rstrip(".") + suffix + "."


def effective_stance(stance: str | None, tilt: float) -> str | None:
    """Stance fuer den ARCHETYP-Tilt (`_select_archetype`): der Regler ist eine
    Aussage darueber, welche Rolle der Spieler im Team spielen WILL - genau die
    Frage, die der Archetyp bei Teil-Gleichstand beantwortet. Bei Tilt 0 bleibt
    die Lage-Stance unveraendert (Byte-Identitaet).

    Die Regel "0 unterscheidende Items -> gar kein Archetyp" (Befund D2) bleibt
    davon unberuehrt: sie steht vor diesem Zweig."""
    if tilt < 0:
        return "defensive"
    if tilt > 0:
        return "aggressive"
    return stance


def class_overlay_active(weights: Weights, confidence: str) -> bool:
    """Feuert das Klassen-Overlay (F3)? Nur auf den defensiven Endstufen
    (`style_tilt <= style_class_tilt`, Default: nur Stufe 1), nur mit aktivem
    Faktor - und nur bei `rich` Kombis.

    Warum nur `rich`: unterhalb davon laedt der KLASSISCHE Klassen-Fallback den
    Pool ohnehin schon (Review Befund 4.3), und der darf seine Kandidaten nicht
    verlieren. Das Overlay ist genau die Ausnahme fuer den umgekehrten Fall -
    die Kombi hat reichlich eigene Daten, das gesuchte Item steht darin nur
    nicht (Gwen und Liandry's)."""
    return (confidence == "rich" and weights.style_class_factor > 0.0
            and weights.style_tilt <= weights.style_class_tilt)


def overlay_usable(name: str, partype: str | None) -> bool:
    """F3-Filter "sinnvoll" fuer einen Klassen-Pool-Kandidaten auf Stufe 1.

    Zwei Kriterien, beide aus vorhandenen Daten pruefbar:

    * **Achse <= 0** - ein weiteres REINES Schadens-Item ist auf "hard
      defensive" per Definition nicht sinnvoll (Annahme aus der Stufen-Semantik,
      plan_spielstil.md F3).
    * **Ressource passt** - Items mit Mana-/ManaRegen-Tag nur fuer Champions mit
      `partype == "Mana"` (Rod of Ages fuer Gwen ja, fuer Briar nein).
      Unbekannter partype (None) filtert NICHT: kein Ausschluss auf Verdacht.

    Der Schadenstyp braucht keine Pruefung - die Klassen-Buckets sind bereits
    nach ihm getrennt (`ad_fighter` / `ap_fighter`), Malignance kann Yorick also
    gar nicht erreichen. Dedupe gegen den Champion-Pool und `items.is_valid_sr`
    macht die aufrufende Schicht wie fuer jeden anderen Kandidaten auch."""
    if style_axis(name) > 0.0:
        return False
    if partype is not None and partype != "Mana":
        return not (items.tags_of(name) & _MANA_TAGS)
    return True


def overlay_entries(entries: list[dict], cid: str | None) -> list[dict]:
    """Die durch `overlay_usable` gefilterten Klassen-Kandidaten eines
    Champions. Eigene Funktion, damit der Ressourcen-Lookup genau einmal je Lauf
    passiert (der Data-Dragon-Zugriff ist gecacht, aber nicht gratis)."""
    partype = champions.partype_for_id(cid)
    return [e for e in entries if overlay_usable(e["item"], partype)]
