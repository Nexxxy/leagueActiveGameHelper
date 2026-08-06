"""Anzeige-Schicht: Pool-Sieger, `now_rel`-Badges, Defensiv-Bruecke, der Filter
"nur plausible naechste Kaeufe" und die Anzeige-/Mess-Reihenfolge der Liste.

Aus recommend.py ausgelagert (Modul-Split, T5).
"""

from . import items
from .rec_boots import _boots_slot_merge
from .rec_context import _RecContext
from .rec_path import _int_slot_dist


def _path_winner(ctx: _RecContext, recs: list[dict]) -> str | None:
    """Sieger des gemeinsamen Kandidatenpools (V2-05) - der Name, der im
    Restpfad-Modus die Rolle uebernimmt, die vorher das Core-Item per Vorfahrt
    hatte. None im Legacy-Modus oder wenn kein Pool-Kandidat uebrig ist.

    Uebersprungen werden Namen ohne Slot-Support JETZT (`path_block`), das
    Anti-Heal-Item und die BOOTS. Anti-Heal ist bewusst eine Option, kein
    Pflichtkauf, und darf auch ueber den Pool nicht zum naechsten Kauf werden.
    Die Boots stehen seit der Listen-Revision zwar auf der Pool-Skala
    (`_boots_pool_entry`), aber ausdruecklich nur fuer Listenrang und `now_rel`:
    ueber die Kaufreihenfolge entscheiden weiter die gewachsenen `_pick_next`-
    Gates (hartes Boots-Gate vor dem ersten Legendary, avg_slot-Vergleich,
    Defensiv-Zweig). Der Kategorie-Score ist bewusst nicht auf diese Frage
    kalibriert - er misst "wie ueblich sind hier Boots", nicht "sind sie JETZT
    dran"."""
    if ctx.weights.path_rescore_factor <= 0.0 or not ctx.path_scores:
        return None
    by_name = {r["item"]: r for r in recs}
    for name, _score in sorted(ctx.path_scores.items(), key=lambda kv: -kv[1]):
        rec = by_name.get(name)
        if (rec is None or rec.get("antiheal") or rec.get("kind") == "boots"
                or name in ctx.path_block):
            continue
        return name
    return None


def _now_rel(ctx: _RecContext, recs: list[dict], next_pick: dict | None) -> None:
    """Setzt `now_rel` (Kaufstaerke RELATIV zur Top-Empfehlung) auf den
    angezeigten Recs und - falls es dazugehoert - auf dem `next`-Dict.

    100 traegt genau der staerkste vergleichbare Kandidat, alle anderen ihren
    Score-Anteil daran: "so nah ist diese Karte am besten Kaufsignal".

    **Warum relativ statt Anteil an der Summe** (Vorgaenger-Feld `now_pct`): der
    normierte Anteil wurde als KONFIDENZ gelesen. "19 % jetzt" auf dem Sieger
    klang nach "nur zu 19 % sicher", waehrend das gross gezeigte Next-Item
    implizit als 100 % gelesen wurde - also genau verkehrt herum. Dazu war die
    Skala listenlaengenabhaengig: mit dem Kandidaten-Cap verteilen sich 100 %
    auf bis zu sieben Karten, und derselbe eindeutige Sieger sah in einer
    langen Liste schwaecher aus als in einer kurzen, ohne dass sich an der
    Datenlage etwas geaendert haette. Die relative Skala haengt nur am
    Verhaeltnis zweier Scores und beantwortet die Frage, die der Spieler
    tatsaechlich stellt: "wie viel schlechter waere die Alternative?".

    Vergleichbar sind NUR die Kandidaten des gemeinsamen Pools (V2-05,
    `path_scores`): Core-Items, Champion-Situationals und - seit der
    Listen-Revision - die empfohlenen Boots als Kategorie
    (`_boots_pool_entry`). Der frueher hier begruendete Boots-Ausschluss
    ("eigene Score-Skala aus V2-03") ist damit aufgehoben: die Boots stehen
    jetzt AUF der Pool-Skala, also traegt ihre Karte ihr Badge wie jede andere
    gewichtete Karte. Ohne Merge-`slot_dist` gibt es weiterhin keinen
    Kategorie-Score - dann bleiben sie automatisch draussen.

    Bewusst OHNE Wert bleiben der Klassen-Fallback (rankt per Design hinter der
    Champion-Evidenz), die Boots-Alternative und der Comp-Hinweis (kein eigener
    Score) sowie die Karten mit einem `_POOL_EXCLUDED_FLAGS`-Flag - die
    reservierte Defensiv-Option und Anti-Heal (Option, kein Pflichtkauf). Eine
    Zahl waere dort erfundene Vergleichbarkeit; die Auswahl ist damit exakt
    dieselbe wie die der Pool-GRUPPE in `_display_order`, sodass Badge und
    Listenrang nicht auseinanderlaufen koennen. Ebenfalls draussen: Namen ohne
    Slot-Support JETZT (`path_block`) - sie koennen den naechsten Kauf gar nicht
    stellen.

    Im Legacy-Modus (path_rescore_factor == 0) gibt es keine Pool-Scores und
    damit das Feld gar nicht."""
    if ctx.weights.path_rescore_factor <= 0.0 or not ctx.path_scores:
        return
    eligible = {}
    for rec in recs:
        name = rec["item"]
        score = ctx.path_scores.get(name)
        if (score is None or score <= 0.0 or rec.get("source") == "class"
                or name in ctx.path_block or _pool_excluded(rec)):
            continue
        eligible[name] = (rec, score)
    if not eligible:
        return
    top = max(score for _rec, score in eligible.values())
    for name, (rec, score) in eligible.items():
        rec["now_rel"] = round(100 * score / top)
        if next_pick and next_pick.get("item") == name:
            next_pick["now_rel"] = rec["now_rel"]


def _defensive_bridge(recs: list[dict], next_pick: dict | None,
                      completed: int) -> None:
    """Stellt die reservierte Defensiv-Option (V2-08) in Bezug zum Next-Item.

    Ohne den Bezug las sich der Block wie eine konkurrierende Anweisung
    ("Defensiv-Option bei Rueckstand: ... wird hinten gekauft"), waehrend oben
    ein Core-Item als naechster Kauf stand - der Spieler sieht zwei Auftraege und
    keine Reihenfolge. Der Zusatz gilt nur vor dem ersten fertigen Item: danach
    ist die Reihenfolge ohnehin offener."""
    if completed > 0 or not next_pick or next_pick.get("kind") != "core":
        return
    for rec in recs:
        if rec.get("defensive_slot"):
            rec["reason"] = (f"Erst {next_pick['item']} fertigbauen - danach "
                             f"zur Absicherung: " + rec["reason"])


# Karten mit einem dieser Flags gehoeren NIE in die Pool-Gruppe von
# `_display_order` - auch dann nicht, wenn ihr Name in `ctx.path_scores` steht.
# Sie stehen nicht dort, weil sie den Score-Wettbewerb gewonnen haben, sondern
# weil eine eigene Schicht sie bewusst hinten anhaengt (Anti-Heal = Option, kein
# Pflichtkauf; Defensiv-Reserve = reservierter Zusatzplatz). Dieselbe
# Ausschluss-Logik wie in `_path_winner`/`_now_rel`.
_POOL_EXCLUDED_FLAGS = ("antiheal", "defensive_slot")


def _pool_excluded(rec: dict) -> bool:
    """True, wenn die Karte trotz Pool-Score nicht in die Pool-Gruppe darf."""
    return any(rec.get(flag) for flag in _POOL_EXCLUDED_FLAGS)


# --- Nur plausible NAECHSTE Kaeufe anzeigen (plan_next_item_only.md) --------
# Produkt-Grundregel (Nutzer-Entscheid 2026-08-04): jede Karte in `items[]` ist
# ein plausibler NAECHSTER fertiger Kauf. Bis hierher war der Slot-Support
# lediglich eine ABWERTUNG - ein Item ohne Support im aktuellen Kaufslot landete
# auf `path_block`, war als `next` gesperrt und rutschte in der gewichteten
# Liste nach hinten, blieb aber sichtbar. Sichtbar heisst fuer den Spieler
# "kaufbar": der Ausloeser war eine Mercury's-Treads-Karte auf Listenplatz 2 in
# Minute 1 (Bel'Veth JUNGLE), wo T2-Boots nie der erste fertige Kauf sind.
#
# Aus der Abwertung wird darum ein ANZEIGE-FILTER. Er wirkt ausschliesslich auf
# `items[]`; `next` (`_pick_next`), die Pool-Scores und `purchase_plan` bleiben
# unberuehrt - dieselbe Trennung wie beim Listen-Umbau. Der `next`-Sieger bleibt
# immer sichtbar (Mess-Kontrakt `replay_profile.replay_candidates` und schlichte
# Konsistenz: was oben gross steht, darf unten nicht fehlen).


def _completed_legendaries(owned_names: set[str]) -> int:
    """Fertige Legendaries im Besitz (>= 2000 G, kein Rezept-Vorprodukt).

    Aus `_pick_next` herausgezogen, weil das harte Boots-Gate jetzt zwei Leser
    hat: die Kaufreihenfolge und die Anzeige-Regel. Bewusst dieselbe (etwas
    grobere) Zaehlung wie bisher in `_pick_next` und NICHT
    `items.count_completed` - das Gate haengt seit jeher an dieser Definition,
    und eine stille Umdefinition waere eine Verhaltensaenderung im Gewand einer
    Refaktorierung."""
    return sum(
        1 for name in owned_names
        if (entry := items.by_name().get(name))
        and entry[1].get("gold", {}).get("total", 0) >= 2000
        and not entry[1].get("into")
    )


def _boots_gate_open(boots: dict | None, completed_owned: int,
                     game_time: float) -> bool:
    """Das HARTE Boots-Gate vor dem ersten Legendary: duerfen fertige Boots
    ueberhaupt der naechste fertige Kauf sein?

    Vor dem ersten Legendary nur fuer echte Boots-first-Champions - die gelernte
    `slot_dist` muss Slot 1 als HAEUFIGSTEN Slot ausweisen (argmax, nicht blosse
    Anwesenheit: Yorick JUNGLE baut Boots of Swiftness in 21 % der Spiele als
    ersten fertigen Kauf, aber in 50 % als zweiten - das ist keine
    Boots-first-Reihenfolge) UND das gelernte Kauf-Timing (`minute.p25`, ohne
    Daten die 10-Minuten-Faustregel) muss erreicht sein. Ab dem ersten fertigen
    Legendary ist das Gate offen; ab da entscheidet der avg_slot-Vergleich in
    `_pick_next`, ob Boots oder Item zuerst kommen.

    EINE Definition fuer zwei Leser (Befund aus plan_next_item_only.md §4.1):
    `_pick_next` fragt "sind Boots jetzt dran?", die Anzeige-Regel fragt "darf
    die Boots-Karte ueberhaupt stehen?". Beide muessen dasselbe Gate meinen -
    sonst kann `next` Boots erzwingen, deren Karte ausgeblendet ist."""
    if boots is None:
        return False
    if completed_owned > 0:
        return True
    clean = _int_slot_dist(boots.get("slot_dist"))
    if not clean or max(clean, key=lambda s: clean[s]) != 1:
        return False
    early = (boots.get("minute") or {}).get("p25")
    return game_time >= (early * 60 if early else 600)


def _boots_slot_supported(ctx: _RecContext) -> bool:
    """Fall (a) der Boots-Trias: hat die gelernte Boots-Slot-Verteilung Support
    am AKTUELLEN Kaufslot?

    Gleiche Semantik wie `_slot_support` bei Items (Anteil im aktuellen Slot
    ueber der Schwelle `boots_slot_min_share`), nur auf der
    Merge-Verteilung der Kategorie (s. `_boots_slot_merge`). Beide
    Neutral-Faelle gelten wie dort als "plausibel", nicht als Ausschluss
    (Plan §6, kein Ausschluss auf Verdacht): jenseits des Datenhorizonts der
    Kombi (`slot_horizon`) ist jede Aussage weggeprunt, und ohne jede
    Merge-Verteilung gibt es ueberhaupt kein Slot-Datum."""
    if ctx.cur_slot > ctx.slot_horizon:
        return True
    _pick_total, merged = _boots_slot_merge(ctx.boots_options or ctx.class_boots)
    if not merged:
        return True
    return merged.get(ctx.cur_slot, 0.0) > ctx.weights.boots_slot_min_share


def _boots_visible(ctx: _RecContext, recs: list[dict],
                   next_pick: dict | None) -> bool:
    """Duerfen die Boots-Karten stehen? TRIAS (Nutzer-Entscheid, Plan §4.1) -
    eine der drei Bedingungen reicht:

    (a) die gelernte Boots-Slot-Verteilung hat Support am aktuellen Kaufslot
        (`_boots_slot_supported`) - der datengetriebene Normalfall;
    (b) das harte Boots-Gate steht an (`_boots_gate_open`): vor dem ersten
        Legendary nur bei echten Boots-first-Champions, danach immer - wer beim
        zweiten fertigen Kauf noch ohne Boots dasteht, soll sie sehen;
    (c) die 300-G-Basis-Boots liegen im Inventar - dann ist das T2-Upgrade ein
        plausibler naechster Abschluss, egal was die Slot-Verteilung sagt.

    Gilt fuer die Boots-Karten GEMEINSAM (Primaervorschlag, Alternative,
    Comp-Hinweis und die Klassen-Boots): sie sind eine Aussage in drei Karten -
    die Alternative ohne ihren Primaervorschlag waere ein Vorschlag ohne
    Empfehlung. Ohne Boots-Karte in `recs` ist die Frage gegenstandslos (True).

    Der Vorrang von `next_pick` ist kein Sonderfall, sondern die Absicherung der
    Invariante: erzwingt die Kaufreihenfolge Boots, sind sie sichtbar. Ueber
    Fall (b) faellt das ohnehin zusammen - der Vorrang haelt es auch dann, wenn
    `_pick_next` ueber einen seiner Rand-Zweige (kein Core-Kandidat) auf Boots
    faellt."""
    boots = next((r for r in recs if r.get("kind") == "boots"), None)
    if boots is None:
        return True
    if next_pick is not None and next_pick.get("kind") == "boots":
        return True
    if _boots_slot_supported(ctx):
        return True
    if _boots_gate_open(boots, _completed_legendaries(ctx.owned_names),
                        ctx.game_time):
        return True
    return any(items.is_base_boots(name) for name in ctx.owned_names)


def _display_blocked(ctx: _RecContext) -> frozenset:
    """Namen, deren Karte KEIN plausibler naechster Kauf ist - die Teilmenge von
    `path_block`, die auf einer echten Slot-AUSSAGE beruht.

    Warum nicht `path_block` selbst: dort landen auch Items, die in einer Kombi
    MIT Slot-Daten selbst weder `slot_dist` noch `avg_slot` haben (der
    Transform-Fall Manamune -> Muramana, s. SLOT_NO_DATA_FIT). Deren `now_ok` ist
    False mangels Datum, nicht wegen eines Befunds - und ein fehlendes Datum darf
    nach Plan §6 nie ausblenden (kein Ausschluss auf Verdacht). Sie bleiben
    darum sichtbar, gedaempft und als `next` gesperrt wie bisher.

    Die `avg_slot`-Rueckfallebene taucht hier nie auf: sie liefert per
    Konstruktion `now_ok=True` (ein Mittelwert ist kein Support-Nachweis, aber
    auch kein Gegenbeweis)."""
    return frozenset(n for n in ctx.path_block if ctx.slot_dist.get(n))


def _next_only_filter(ctx: _RecContext, recs: list[dict],
                      next_pick: dict | None) -> list[dict]:
    """Wirft aus `items[]`, was kein plausibler NAECHSTER fertiger Kauf ist.

    Drei Ausnahmen, jede aus der entschiedenen Politik:

    * **Support-Finals** (`support_final`, Schicht 5) - eine fast
      deterministische Quest-Wahl ausserhalb der Slot-Statistik. Statt sie zu
      filtern, wurde ihre AKTIVIERUNG geschaerft (`_support_final`: erst ab
      Bounty of Worlds, der letzten Quest-Stufe).
    * **Der `next`-Sieger** - er steht oben gross; ihn aus der Liste zu werfen
      waere ein Widerspruch in sich und wuerde den Mess-Kontrakt
      `replay_candidates = [next] + items` brechen.
    * **Leerlauf-Schutz** (Plan §6): bleibt nichts uebrig, bleibt der
      Core-Vorschlag stehen (sonst die erste Karte). Eine leere Liste ist fuer
      den Spieler wertloser als eine zeitlich untypische Karte - dieselbe
      Abwaegung wie im letzten Zweig von `_pick_next`.

    Alles andere laeuft durch denselben Filter, auch die beiden Jetzt-Optionen
    (Anti-Heal, reservierte Defensiv-Option): sie sind Optionen fuer JETZT, und
    genau das prueft die Regel (Plan §4.2). Ausgeschaltet (`next_only_display`
    False) oder im Legacy-Anzeigemodus (`display_legacy`, der bewusst die
    Alt-Anzeige misst) gibt die Funktion die Liste unveraendert zurueck - sonst
    maesse der A/B zwei Umbauten gleichzeitig."""
    if not ctx.weights.next_only_display or ctx.weights.display_legacy:
        return recs
    blocked = _display_blocked(ctx)
    boots_ok = _boots_visible(ctx, recs, next_pick)
    keep = next_pick.get("item") if next_pick else None
    out: list[dict] = []
    for rec in recs:
        if rec.get("support_final") or rec["item"] == keep:
            out.append(rec)
        elif rec.get("kind") == "boots":
            if boots_ok:
                out.append(rec)
        elif rec["item"] not in blocked:
            out.append(rec)
    if out:
        return out
    core = [r for r in recs if r.get("kind") == "core"]
    return core[:1] or recs[:1]


def _display_order(ctx: _RecContext, recs: list[dict]) -> list[dict]:
    """Bringt die Empfehlungsliste in ihre Anzeige-/Mess-Reihenfolge: ein fester
    Listen-KOPF in gelernter Bau-Reihenfolge, dahinter die gewichtete
    Pool-Sortierung, ganz hinten das Nicht-Vergleichbare.

    Vier Gruppen, in dieser Reihenfolge:

    1. **Support-Finals** (Schicht 5): Karten mit dem HERKUNFTS-Flag
       `support_final` aus `_support_final` - eine fast deterministische
       Sonderwahl ohne Pool-Score, die vorn bleibt wie bisher.
    2. **Kopf**: der erste `kind=="core"`-Eintrag (Support-Finals zaehlen nicht
       mit, sie stehen ja schon in Gruppe 1) und ALLE `kind=="boots"`-Eintraege
       ausser dem Klassen-Fallback (`source == "class"`) - also
       Primaervorschlag, Alternative und Comp-Hinweis. Sie behalten ihre
       ORIGINALE relative Reihenfolge aus `recs`, damit Core vor Boots und
       Primaer vor Alternative vor Hinweis steht.
    3. **Pool-Kandidaten** (`ctx.path_scores`, V2-05): alles Uebrige mit
       Pool-Score, AUSSER den Karten mit einem `_POOL_EXCLUDED_FLAGS`-Flag
       (s. u.). Core-Items, Champion-Situationals und die empfohlenen Boots
       (als Kategorie, s. `_boots_pool_entry`) laufen dort auf EINER Skala, also
       duerfen sie gegeneinander sortiert werden - absteigend nach Score.
       `path_block`-Namen und Scores <= 0 bleiben in dieser Gruppe (sie sind
       Champion-Evidenz), landen durch die Sortierung aber hinten.
    4. **Rest** in unveraenderter relativer Reihenfolge: Klassen-Boots,
       Klassen-Fallback, Defensiv-Reserve (`defensive_slot`), Anti-Heal
       (`antiheal`). Sie haben keinen bzw. einen nicht vergleichbaren Score; sie
       einzusortieren waere erfundene Vergleichbarkeit - dieselbe Begruendung
       wie beim `now_rel`-Ausschluss.

    **Warum `antiheal`/`defensive_slot` per FLAG draussen bleiben, nicht nur
    mangels Score** (`_POOL_EXCLUDED_FLAGS`, gleiche Bedingung wie in
    `_path_winner`/`_now_rel`): Beide Karten koennen einen Namen tragen, der im
    Pool steht. Die Anti-Heal-Schicht entfernt den regulaeren
    Situational-Eintrag gleichen Namens und haengt ihre Karte ans Ende - der
    Pool-Score des Namens bleibt dabei bestehen. Ueber den Namens-Match sortierte
    die Vorgaenger-Fassung sie mitten in die Pool-Gruppe, teils weit nach vorn,
    und verdraengte dort das Top-Situational aus der gemessenen Top-3
    (`replay_candidates`). Der Same-Data-Backtest (197.664 Samples) mass dafuer
    auf allen UTILITY-Kombis -5 bis -30 pp Hit@3 (Support-Anti-Heal Chemtech
    Putrifier hat hohe Pick-Raten); in der Anzeige VOR dem Umbau stand die Karte
    immer ganz hinten. Die Defensiv-Reserve ist derselbe Fall in schwach: ihr
    Score liegt per Konstruktion unter den gezeigten Kandidaten, aber sie steht
    im Block, weil ein Platz RESERVIERT wurde - nicht, weil sie den
    Score-Wettbewerb gewonnen hat. Beide bekommen deshalb auch `pool: False`:
    das Flag markiert die Gruppenzugehoerigkeit fuer den Frontend-Trenner, nicht
    die blosse Existenz eines Scores.

    **Warum Gruppe 1 an der HERKUNFT haengt und nicht am Namen** (gleicher
    Fehlertyp wie oben, gemessen auf Holdout-Samples): die fuenf Endformen
    (Celestial Opposition, Solstice Sleigh, Zaz'Zak's Realmspike, Dream Maker,
    Bloodsong) stehen seit der `classify_items`-Erweiterung auch als ganz
    normale Core-/Situational-Kandidaten MIT Pick-Rate in der Wissensbasis. Bei
    Supports werden sie also regulaer gescort und tauchen als
    `kind=="situational"`-Karten auf, AUCH wenn die Support-Final-Schicht
    (`_support_final`) gar nicht aktiv ist (Quest schon abgeschlossen oder nie
    getragen). Ein Match auf `COMPLETED_SUPPORT_ITEMS` riss diese regulaeren
    Karten vor den Kopf und verdraengte Core/Boots/Top-Situational aus der
    gemessenen Top-3 (`replay_candidates`) - eine Sample-Probe mass -5 bis
    -30 pp Hit@3 auf praktisch allen UTILITY-Kombis (80 von 81 Samples
    divergierten, 19:1 Treffer-Flips gegen die Umbau-Fassung). In der Anzeige
    VOR dem Umbau standen diese Karten schlicht score-sortiert im
    Situational-Block. Nur die Schicht-Recs tragen `support_final`; regulaere
    Karten mit demselben Namen bleiben damit in Pool bzw. Rest, wo ihr Score sie
    hinsetzt. Der Dedupe in `recommend()` bleibt davon unberuehrt namensbasiert
    (bei AKTIVER Schicht ist sie die einzige Quelle fuer die fuenf Endformen).

    WARUM der Kopf statt Positions-Floors (Backtest-Beleg): die Vorgaenger-
    Fassung sortierte zuerst nach Score und hob Boots und Core danach per
    Positions-Floor wieder nach vorn - gezaehlt aber HINTER den Support-Finals.
    Stehen dort waehrend der Support-Quest bis zu zwei Endformen, rutscht das
    Core-Item auf absolute Position 5-6 und faellt aus der gemessenen Top-3
    (`replay_candidates` = `[next] + items`, dedupliziert, Hit@3 = die ersten
    drei), waehrend die Vorgaenger-Anzeige es auf absoluter Position 3 hatte.
    Der Same-Data-A/B mass daher auf allen UTILITY-Kombis -17 bis -30 pp Hit@3
    bei stabiler Restpopulation.

    Der feste Kopf loest das strukturell statt per Zaehlregel: Gruppe 1 + 2 sind
    Stueck fuer Stueck DIESELBEN Eintraege in DERSELBEN Reihenfolge wie die
    Bau-Reihenfolge von `recommend()` (Support-Finals, Core, Boots), die als
    `display_legacy` die alte Anzeige ist. Der Praefix ist damit
    positionsgleich, die gemessene Top-3 per Konstruktion identisch zur alten
    Anzeige - und die Gewichtung wirkt ab der ersten Position NACH dem Kopf,
    wo sie nichts mehr kosten kann. Es ist weiterhin eine reine Umstellung der
    Ausgabe-Reihenfolge: Score, `next`, `now_rel` und `pool` bleiben unberuehrt
    (die `now_rel`-Badges fallen dadurch nur noch innerhalb der Pool-Gruppe
    monoton).

    Die Mess-Invariante "der ERSTE `kind=="boots"`-Eintrag ist der
    Primaervorschlag" (Boots-Metrik in Backtest und Post-Game-Check) haelt
    unabhaengig von jedem Score: alle Nicht-Klassen-Boots stehen im Kopf in
    ihrer Bau-Reihenfolge, und dort kommt der Primaervorschlag zuerst.

    Jeder Eintrag bekommt `pool: True/False`, damit das Frontend den Schnitt
    zwischen Gruppe 3 und 4 sichtbar machen kann; die Kopf-Eintraege
    zusaetzlich `head: True`, damit ein Kopf-Eintrag ohne Pool-Score den
    Trenner "Weitere Optionen" nicht verfrueht ausloest. Kopf und `pool`
    schliessen sich NICHT aus - Core und gescorte Boots tragen beides. Der
    `next`-Sieger BLEIBT in der Liste (Mess-Kontrakt
    `replay_profile.replay_candidates`); ausgeblendet wird er nur beim
    Rendern.

    `Weights.display_legacy` schaltet den gesamten Umbau ab (Ablation): dann
    kommen die Recs unveraendert in ihrer Bau-Reihenfolge zurueck - ohne
    Sortierung, ohne Kopf und ohne `pool`/`head`-Felder."""
    if ctx.weights.display_legacy:
        return recs
    support: list[dict] = []
    tail: list[dict] = []
    for rec in recs:
        rec["pool"] = rec["item"] in ctx.path_scores and not _pool_excluded(rec)
        (support if rec.get("support_final") else tail).append(rec)
    core = next((r for r in tail if r.get("kind") == "core"), None)
    # Identitaet statt Gleichheit: zwei inhaltsgleiche Dicts sind fuer `in`
    # dasselbe Element, fuer die Liste aber zwei Karten.
    head = [r for r in tail
            if r is core or (r.get("kind") == "boots"
                             and r.get("source") != "class")]
    head_ids = {id(r) for r in head}
    for rec in head:
        rec["head"] = True
    graded = [r for r in tail if id(r) not in head_ids]
    # `sorted` ist stabil: bei Score-Gleichstand bleibt die bisherige Reihenfolge.
    pool = sorted((r for r in graded if r["pool"]),
                  key=lambda r: -ctx.path_scores[r["item"]])
    rest = [r for r in graded if not r["pool"]]
    return support + head + pool + rest
