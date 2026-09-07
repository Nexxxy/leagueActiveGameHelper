"""Stellschrauben der Empfehlungs-Engine: `Weights`-Dataclass, `DEFAULT_WEIGHTS`
und die Score-/Schwellen-Konstanten der Datenhygiene.

Aus recommend.py ausgelagert (Modul-Split, T1). Bewusst ohne jeden
Engine-internen Import - das Modul ist die unterste Schicht und wird von allen
`rec_*`-Satelliten gelesen.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Weights:
    """Zentrale Stellschrauben fuer das Ranking der situativen Items.

    Der Offline-Backtest schaltet einzelne Schichten ab, indem er das jeweilige
    Gewicht auf 0 setzt (Ablation); kuenftiges Tuning aendert nur noch diese
    Werte.

    Die Stance-SCORE-Schicht gibt es hier nicht mehr (Review-Befund D,
    2026-07-13): die lage-konditionierte Backtest-Auswertung (by_stance) hat
    gezeigt, dass der Stance-Score-Eingriff selbst auf seinem Ziel-Subset
    (defensive Stance + Sieger, n=440) NICHT gewinnt - Hit@3 54,3 % (aktiv) vs.
    55,5 % (aus), Hit@1 28,6 % vs. 40,5 % - und die Engine insgesamt unter die
    Baseline drueckt. Die Gewichte lagen danach dauerhaft auf 0/False und sind
    beim Testsuite-Review 2026-08-04 samt Code entfallen. Die Stance selbst
    bleibt: `own_stance`, `stance`/`stance_reason` (Badge/Text im
    Frontend) und der Archetyp-Tilt (`_select_archetype`) sind unveraendert -
    sie greift nur nicht mehr ins Ranking oder in die Kaufreihenfolge ein.

    Nach demselben Muster gibt es hier auch keine Keystone-SCORE-Schicht mehr
    (Nutzer-Entscheid 2026-09-06): das Gate war auf zwei unabhaengigen Patches
    nicht bestanden - die Rangfolge der Sweep-Werte kippte zwischen 16.14 und
    16.15, ein Effekt mit datensatzabhaengigem Vorzeichen ist Rauschen. Die
    Hauptrune beeinflusst den Build praktisch nicht; gebaut wird ohnehin
    weitgehend gleich. Die DATENLAGE bleibt: `pipeline/aggregate.py` fuehrt die
    `by_keystone`-Zellen weiter in die KB und `pipeline/matchindex.py` das
    `ks`-Feld - nur liest die Engine sie nicht mehr.

    Die Lage-Schicht (`by_state`) ist denselben Weg NICHT ganz gegangen: sie ist
    seit dem Nutzer-Entscheid 2026-09-06 per Modus-Schalter `state_factor`
    stillgelegt (Default 0.0), aber vollstaendig erhalten - Zellen, Lookup,
    Zuschlag und Text stehen unveraendert da und lassen sich mit einem Wert > 0
    wieder einschalten, sobald es eine neue Hypothese zu messen gibt.
    Begruendung des Defaults am Feld.

    Die Gegnerschaden-Schicht (`by_threat`) folgt seit dem Nutzer-Entscheid
    2026-09-07 exakt demselben Muster: Modus-Schalter `threat_scale`, Default
    0.0 = aus, Code und Zellen bleiben vollstaendig erhalten. Anders als bei
    Stance und Keystone war die Abschalt-Regel hier VORAB festgelegt
    (plan_lage_threat.md F5a) und wurde auf drei Patches gemessen; die Zahlen
    stehen am Feld. Wichtig fuer die Abgrenzung: was der Nutzer auf den Karten
    als "gegen dieses Gegnerteam" liest, kommt ganz ueberwiegend NICHT aus
    dieser Schicht, sondern aus den regelbasierten Sachtexten in `explain_item`
    ("Ruestung gegen die 58% AD im Gegnerteam", Top-Threat-Nennung) und aus der
    Boots-Schicht - beide haengen am threat-gewichteten `split` bzw. an
    `boots_by_threat` und sind vom Schalter unberuehrt."""
    # --- Gegnerschaden-Schicht (by_threat) ---------------------------------
    # `threat_scale` ist ein MODUS-Schalter, kein reiner Skalar (Muster
    # `state_factor`/`heal_factor`):
    #   0.0  = die Schicht ist KOMPLETT aus. `_conditional_layers` laedt die
    #          by_threat-Zelle gar nicht erst - es gibt also weder einen
    #          Score-Zuschlag NOCH den Win-Rate-Text ("62% Win gegen AD-lastige
    #          Teams"). Der Text haengt bewusst an der Zelle und nicht am Cap:
    #          ein Cap 0 haette die Wirkung genommen und die Behauptung
    #          trotzdem stehen gelassen.
    #   >0.0 = die Schicht wirkt, der Faktor skaliert den Zuschlag;
    #          0.8 = exakt das Verhalten vor dem Schalter.
    # `threat_cap` bleibt davon unberuehrt der Deckel des Zuschlags.
    #
    # Default 0.0 (Nutzer-Entscheid 2026-09-07 nach der VORAB festgelegten
    # Regel aus plan_lage_threat.md F5a; Details im Entscheidungs-Log).
    # Gemessen wurde `threat_cap = 0.0` (aus) gegen `0.2` (an) ueber alle
    # KB-Kombis auf drei Patches - jeweils Hit@3-Vorteil von "aus":
    #   16.15: gesamt +0,10 pp, Pool +-0,00
    #   16.16: gesamt +0,10 pp, Pool +0,37 pp (Gwen +0,6)
    #   16.17: gesamt +-0,00 pp, Pool +0,13 pp
    # "aus" ist damit auf ALLEN drei Patches global nicht schlechter, und kein
    # Pool-Champion verliert auf einem Patch mehr als 0,1 pp - die Regel
    # ("Mehrheit aus drei Patches, kein Pool-Champion > 1 pp Verlust") ist
    # erfuellt. Dazu passt die Datenanalyse 2026-09-06 (12 Jungler, zwei
    # Patches): die Item-Wahl haengt schon ROH kaum von der Gegnerteam-
    # Klassifikation ab (Cramers V 0,00-0,08). Das Gegnerteam entscheidet ueber
    # die Resistenz-ART - und genau das sagen die Regeltexte ohnehin -, nicht
    # ueber die Item-WAHL.
    #
    # Die DATENLAGE bleibt: `pipeline/aggregate.py` fuehrt die by_threat-Zellen
    # unveraendert in die KB, die Schicht ist mit einem Wert > 0 sofort wieder
    # da. Unberuehrt sind ausserdem `boots_by_threat` (eigene Schicht, eigener
    # Schalter `boots_kb_factor`) und die regelbasierten Sachtexte aus
    # `explain_item`.
    threat_cap: float = 0.2           # Cap des by_threat-Schubs (+/-)
    threat_scale: float = 0.0         # Modus-Schalter (s.o.), 0.8 = alt
    # --- Lage-Schicht (by_state) -------------------------------------------
    # `state_factor` ist ein MODUS-Schalter, kein reiner Skalar (Muster
    # `heal_factor`/`next_after_factor`):
    #   0.0  = die Schicht ist KOMPLETT aus. `_conditional_layers` laedt die
    #          by_state-Zelle gar nicht erst - es gibt also weder einen
    #          Score-Zuschlag NOCH einen Begruendungstext ("... wenn du
    #          vorne/hinten liegst"). Der Text haengt bewusst an der Zelle und
    #          nicht am Cap: ein Cap 0 haette die Zahl weggenommen und die
    #          Behauptung stehen lassen.
    #   >0.0 = die Schicht wirkt, der Faktor skaliert den Zuschlag;
    #          1.0 = exakt das Verhalten vor dem Schalter.
    # `state_cap` bleibt davon unberuehrt der Deckel des Zuschlags.
    #
    # Default 0.0 (Nutzer-Entscheid 2026-09-06, Details im Entscheidungs-Log).
    # Zwei unabhaengige Befunde zeigen in dieselbe Richtung:
    #   1. Backtest-Sweep 16.15 ueber 197.724 Kaeufe: die Schicht AN kostet
    #      0,6 pp Hit@3, und 0,1/0,2/0,4 liefern bit-identische Reports - der
    #      Cap greift nie, das Feld war faktisch ein Boolean.
    #   2. Datenanalyse 2026-09-06 ueber 12 Jungler auf zwei Patches: Cramers V
    #      Item x Gold-Lage liegt INNERHALB von Kaufslot und Vorgaenger-Item nur
    #      bei 0,04-0,09. High-Elo kauft im Rueckstand dieselben Items, nur
    #      frueher im Build - die Lage verschiebt den ZEITPUNKT, nicht die WAHL.
    # Der Gold-Zustand selbst bleibt unberuehrt erhalten (`gold_state`): er
    # traegt weiter das Verdikt, die Boots-Opposition, die Backtest-
    # Stratifizierung und die Defensiv-Reserve (die auf den by_state-Behind-
    # KAUFZAEHLUNGEN arbeitet, nicht auf diesem Win-Rate-Zuschlag).
    state_factor: float = 0.0
    state_cap: float = 0.2            # Cap des by_state-Schubs (+/-)
    synergy_factor: float = 0.3       # Faktor fuer _synergy_boost
    redundancy_penalty: float = 0.3   # Abzug bei redundantem Sustain-Stack
    use_archetypes: bool = True       # False -> _select_archetype ueberspringen
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
    # KB-datengetriebener Heal-Schub (by_heal in builds.yaml): wirkt NUR, wenn das
    # Gegnerteam >= HEAL_HEAVY_MIN Champions der kuratierten Heiler-Liste hat
    # (`rec_context._enemy_heal_bucket`) und die Kombi `rich` ist. Mechanik 1:1 wie
    # by_threat (Shrinkage gegen base_win_rate, RANK_MIN_N-Zellgate, Cap).
    #
    # `heal_factor` ist ein MODUS-Schalter, kein reiner Skalar (Muster
    # `next_after_factor`): 0.0 = die Schicht ist KOMPLETT aus - es wird nicht
    # einmal die Zelle geladen, es gibt keinen Schub UND keinen Begruendungstext.
    # >0.0 skaliert den Schub wie `threat_scale`.
    #
    # Default 0.0: die Zellen kommen mit der Dimension zwar immer in die KB
    # (Datenlage), die Wirkung schaltet aber erst ein bestandenes Backtest-Gate
    # frei (Pool-Mittel Hit@3 muss steigen, kein Pool-Champion darf > 1 pp
    # fallen) - dieselbe Beweislast wie bei next_after_factor. Solange hier 0
    # steht, ist der Nachweis NICHT erbracht.
    heal_factor: float = 0.0
    heal_cap: float = 0.2             # Cap des by_heal-Schubs (+/-)
    # next_after-Bigramm-Lift (T2/T3, plan_roadmap.md "Validierung 2026-07-29"):
    # 0.0 = AUS (Lift bleibt exakt 1.0), 1.0 = voller Lift, Zwischenwerte
    # daempfen ihn (siehe _next_after_lift).
    # Wert 0.5 aus dem T3-Gate (Offline-Backtest 16.14, Holdout 6127 Matches,
    # 4159 Samples, Sweep 0/0.5/1/2 auf identischer Datenbasis): 0.5 ist der
    # einzige Wert, bei dem KEIN Pool-Champion bei Hit@3 verliert (Briar +3,
    # Gwen +5, Shyvana +1, Yorick +/-0 Treffer) und Hit@1 zulegt (+15). Groessere
    # Faktoren kaufen Gwen-Gewinne mit Briar-/Shyvana-Verlusten. Der Effekt ist
    # klein (Pool-Hit@3 +0,18 pp) - halber statt voller Lift ist genau deshalb
    # die ehrliche Einstellung: das Signal darf mitreden, nicht dominieren.
    next_after_factor: float = 0.5
    # --- Boots-Scoring (V2-03, plan_engine_v2.md) --------------------------
    # boots_kb_factor ist ein MODUS-Schalter, kein reiner Skalar:
    #   0.0  = die alte Regelkaskade (viel CC -> Tenacity, sonst AD/AP-Konter,
    #          sonst die meistgespielten) - exakt das Verhalten vor V2-03,
    #          damit die Ablation "Faktor 0 = alt" sauber messbar bleibt.
    #   >0.0 = Score-Modus: Basis ist die champion-spezifische Pick-Rate, die
    #          Comp-Signale sind nur noch gedeckelte Schuebe darauf. Der Faktor
    #          skaliert dabei ALLE Schuebe (empirische Zellen UND Regel-Priors),
    #          1.0 = volle Staerke.
    # Wert 1.0 aus dem V2-03-Gate (Offline-Backtest 16.15, Holdout 5275 Matches,
    # 38.340 Samples davon 9.914 Boots-Kaeufe, Sweep 0/0,5/1/2 auf identischer
    # Trainings-KB): die Boots-Wahl trifft den tatsaechlichen Kauf primaer in
    # 59,5 % statt 24,4 % der Faelle (Hit@1 gesamt 31,6 % -> 40,1 %, Hit@3
    # 68,0 % -> 68,4 %), der Item-Pfad bleibt mit Item-Hit@3 67,6 % exakt
    # unberuehrt - die Schicht bewegt genau das, was sie bewegen soll. 0,5 ist
    # praktisch gleichwertig (59,3 %), 2,0 faellt wieder ab (58,4 %): die
    # gedeckelten Schuebe brauchen keine zusaetzliche Verstaerkung, die Deckel
    # SIND die Daempfung. Per Champion+Rolle (212 Kombis mit n >= 50) fallen nur
    # 3 um mehr als 1 pp Hit@3, schlimmster Fall -1,9 pp.
    boots_kb_factor: float = 1.0
    boots_pick_cap: float = 0.2       # Cap der konditionalen Pick-Verschiebung
    boots_win_cap: float = 0.15       # Cap der konditionalen Win-Verschiebung
    boots_win_scale: float = 0.8      # Skalierung der Win-Verschiebung
    boots_prior_cap: float = 0.15     # Cap der Regel-Priors (CC/AD/AP) zusammen
    boots_comp_hint: bool = True      # Comp-Warnhinweis als eigener Vorschlag
    # --- Restpfad-Neubewertung (V2-05, plan_engine_v2.md) ------------------
    # path_rescore_factor ist ebenfalls ein Modus-Schalter:
    #   0.0  = altes Verhalten (statische Core-Reihenfolge nach avg_slot, kein
    #          Slot-Support-Filter).
    #   >0.0 = gemeinsamer Kandidatenpool (restliche Core + Situationals): der
    #          Slot-Support P(Slot|Item) am aktuellen Slot daempft den Basisterm,
    #          Items ohne Support am aktuellen Slot sind als `next` gesperrt,
    #          Core-Status ist nur noch ein Prior-Bonus. Der Faktor daempft
    #          die Slot-Gewichtung linear Richtung neutral (1.0 = voll).
    # Dass die Schicht ueberhaupt gebraucht wird, steht seit dem V2-05-Gate fest
    # (Offline-Backtest 16.15, 38.340 Samples): 0,0 -> 0,6 hob Hit@3 64,3 % ->
    # 68,4 %, Item-Hit@3 61,9 % -> 67,6 %, Hit@1 36,5 % -> 40,1 % und den 3.+
    # Kauf - den Zielfall, weil dort das uebersprungene Core-Item festklebte -
    # 56,7 % -> 63,6 %. Die Frage war nur noch die Staerke.
    #
    # Wert 0.8 aus der Nachkalibrierung 2026-08-04 (Sweep auf 197.724 frischen
    # Samples, Patch 16.15, `report-sweep-path_rescore_factor.yaml`): 0,8
    # gewinnt gegenueber 0,6 im Pool-Mittel auf ganzer Breite - Hit@3 69,4 % ->
    # 69,9 %, Item-Hit@3 68,3 % -> 68,9 %, Hit@1 40,1 % -> 40,4 % - und, das ist
    # der Punkt, JETZT AUCH ab dem 3. Kauf (65,1 % -> 65,4 %). Genau dieser Fall
    # war auf der alten, zehnmal duenneren 38k-Basis noch das Argument FUER 0,6
    # (dort fiel 0,8 auf 63,3 %); auf frischen Daten ist die Gegen-Evidenz
    # gedreht, die alte 0,6-Begruendung damit abgeloest.
    #
    # Preis: 57 von ~370 Kombis (n >= 50) verlieren mehr als 1 pp Hit@3,
    # Extremfall Viego JUNGLE -13,8 pp. Bewusster Nutzer-Entscheid pro
    # Pool-Optimierung: der Helper ist ein Ein-Personen-Tool, die Verlierer sind
    # Nicht-Pool-Champions, und der Fokus-Pool selbst gewinnt (Gwen JUNGLE
    # +1,0 pp, Shyvana JUNGLE +1,0 pp, Briar JUNGLE +0,5 pp; nur Yorick JUNGLE
    # -0,6 pp). Wer breit statt auf den Pool optimieren will, setzt 0,6.
    # 1,0 faellt weiterhin auch im Mittel ab: die harte Slot-Daempfung schiebt
    # dann Items weg, die im Nachbarslot voellig normal sind.
    path_rescore_factor: float = 0.8
    path_core_bonus: float = 0.15     # Prior-Bonus fuer Core-Status im Pool
    # Was passiert mit einem Item, dessen `slot_dist` VOR dem aktuellen Kaufslot
    # endet (mehr als SLOT_LATE_TOLERANCE dahinter)? False (Default): Hard-Drop
    # aus dem Kandidatenpool (V2-05). True: es bleibt Kandidat, maximal
    # gedaempft und als `next` gesperrt - nur noch als Experiment-/Ablations-
    # Schalter erhalten, weil der Backtest den Hard-Drop bestaetigt hat
    # (report-sweep-slot_late_keep.yaml: True kostet 0,3 pp Hit@3 gesamt und
    # 0,7 pp ab dem 3. Kauf). Bewusst ein Bool und kein Gewicht - die Frage ist
    # "Datenluecke oder Aussage?", keine Feinjustierung. Ausfuehrliche
    # Begruendung am Fundort in `_slot_support`.
    slot_late_keep: bool = False
    # Gelernte Exklusiv-Paare (V2-04, `exclusive` in der KB) als HARTER
    # Kandidaten-Ausschluss zusaetzlich zu items.conflicts. Bewusst ein Bool und
    # kein Score-Gewicht: das ist eine Korrektheitsregel ("die beiden baut
    # niemand zusammen"), keine Ranking-Stellschraube. Abschaltbar, damit die
    # Ablation sie einzeln messen kann.
    learned_exclusive: bool = True
    # --- Behind-Situationals (V2-08, plan_engine_v2.md Konzept 3) ----------
    # Reserviert bei defensiver Stance ODER scharfem Todes-Signal EINEN Slot im
    # situativen Block fuer die beste defensive Option (Quellen-Kaskade
    # Champion-Behind-Zelle -> Klassen-Behind-Fallback -> DEF_TAGS-Overlay).
    # Bewusst ein Bool: die Reservierung ist eine Anzeige-Entscheidung ("zeig
    # ihm nicht drei Glass-Cannon-Items, wenn er gerade dreimal gestorben ist"),
    # kein Score-Gewicht. Der HAUPTVORSCHLAG (`next`) bleibt davon unberuehrt -
    # die Mehrheit gewinnt, auch wenn das Rabadon ist (Nutzer-Vorgabe,
    # plan_engine_v2.md Abschnitt 2). False = Ablations-/Alt-Verhalten.
    defensive_slot: bool = True
    # --- Anzeige-Reihenfolge (gewichtete Liste) ----------------------------
    # Reiner ABLATIONS-/VERGLEICHSSCHALTER fuer Same-Data-A/B, kein Tuning-Wert:
    #   False (Default) = die gewichtete Liste (`_display_order`): fester
    #          Listen-Kopf (Core + Boots) und dahinter die Pool-Sortierung nach
    #          `path_scores`.
    #   True  = Anzeige-Reihenfolge VOR dem Listen-Umbau. `_display_order` gibt
    #          die Recs unveraendert zurueck - die Bau-Reihenfolge von
    #          `recommend()` (Support-Finals, Core, Boots, Klassen-Boots,
    #          Situationals, Anti-Heal) IST die alte Anzeige. Es entstehen dann
    #          weder `pool`- noch `head`-Felder.
    # Bewusst ein Bool: die Frage ist "alte oder neue Liste?", keine
    # Feinjustierung. Alles andere (Pool-Scores, `next`, `now_rel`) laeuft
    # unveraendert weiter - der Schalter stellt NUR die Ausgabe-Reihenfolge um,
    # damit der Backtest beide Anzeigen auf identischer Datenbasis misst.
    display_legacy: bool = False
    # --- Nur plausible NAECHSTE Kaeufe anzeigen ----------------------------
    # Produkt-Grundregel (Nutzer-Entscheid 2026-08-04, plan_next_item_only.md):
    # jede Karte in `items[]` ist ein plausibler NAECHSTER fertiger Kauf. Karten
    # fuer spaetere Slots werden nicht mehr nur nach hinten sortiert, sondern
    # AUSGEBLENDET - ein T2-Boots-Vorschlag in Minute 1 untergraebt das Vertrauen
    # in die ganze Liste. Umfang der Regel (alles unter diesem einen Schalter):
    #   1. Items ohne Slot-Support JETZT (`path_block` MIT Slot-Daten) fliegen
    #      aus `items[]` (Situationals, Core, Klassen-Fallback, Anti-Heal,
    #      Defensiv-Reserve - kein Sonderstatus).
    #   2. Die Boots-Karten (Primaer + Alternative + Comp-Hinweis, auch die
    #      Klassen-Boots) erscheinen nur unter der Trias aus `_boots_visible`.
    #   3. Die Support-Endform-Karte erst ab der LETZTEN Quest-Stufe
    #      (Bounty of Worlds), nicht mehr ab irgendeinem Questketten-Item.
    #
    # True (Default) = die Regel gilt. False = die ungefilterte Alt-Anzeige,
    # Stueck fuer Stueck wie vor der Regel - reiner ABLATIONS-/Messschalter fuer
    # den Same-Data-A/B (Muster wie `display_legacy`), kein Tuning-Wert und kein
    # Veto: der A/B kalibriert die Schaerfe und dokumentiert den Preis, die Regel
    # selbst gilt unbedingt (Plan §4.4 "Regel schlaegt Metrik").
    #
    # Erwartung an die Zahlen: Hit@3 kann sinken, wo der REALE Kauf zeitlich
    # untypisch frueh war (echte Early-Boots) - der Backtest misst
    # `[next] + items`, und eine ausgeblendete Karte kann nicht mehr treffen.
    # Diese Luecke wird bewusst NICHT kompensiert; sie ist der ehrliche Preis.
    next_only_display: bool = True
    # Mindest-Anteil der gelernten Boots-Slot-Verteilung am AKTUELLEN Kaufslot,
    # damit Fall (a) der Boots-Trias (`_boots_slot_supported`) zaehlt.
    #
    # 0.0 ist exakt die Plan-Semantik "Support = Anteil > 0", identisch zur
    # Item-Seite (`_slot_support`). Der Wert ist die KALIBRIERSCHRAUBE, die
    # plan_next_item_only.md §5 dem Same-Data-A/B ueberlaesst - denn auf den
    # echten 16.15-Daten ist "Anteil > 0" bei Slot 1 noch sehr weich: die
    # pick-gewichtete Merge-Verteilung fuehrt dort Bel'Veth JUNGLE mit 0,6 %,
    # Shyvana JUNGLE mit 1,1 %, Briar JUNGLE mit 1,5 % und Gwen JUNGLE mit
    # 4,3 % - Rest-Anteile knapp ueber der Pruning-Schwelle, die die Boots-Karte
    # in Minute 1 stehen lassen. Ein echter Boots-first-Champion liegt klar
    # darueber (Yorick TOP: 34,1 %). Der Ausloeser der ganzen Regel war genau
    # dieser Fall (Bel'Veth JUNGLE, Minute 1-3).
    #
    # Default 0.05 (Nutzer-Entscheid 2026-08-04 nach dem Sweep
    # `--sweep boots_slot_min_share=0,0.02,0.05,0.1`): filtert auch Gwens
    # 4,3 % weg - nur echte Boots-first-Faelle (Yorick TOP) zeigen die Karte
    # frueh. Gemessener Preis im Pool: Hit@3 -0,3 pp, Boots-prim. -1,2 pp
    # (global praktisch +-0) - bewusst bezahlt, "Regel schlaegt Metrik".
    # Gemessen wird absolut (Anteil an allen Boots-Kaeufen dieser Kombi), nicht
    # relativ zum Peak: "wie viele schnueren die Boots in DIESEM Slot" ist die
    # Frage, die die Karte beantworten muss.
    boots_slot_min_share: float = 0.05
    # --- Spielstil-Regler (plan_spielstil.md) ------------------------------
    # Bewusste NUTZER-Vorgabe, keine Lage-Erkennung (die ist die Stance, s. o.
    # Befund D): -1.0 = Regler-Stufe 1 "hard defensive/tanky", 0.0 = Stufe 3
    # neutral (byte-identisch zum Verhalten ohne Regler), +1.0 = Stufe 5
    # "Glass Cannon". Der Server bildet die fuenf Stufen darauf ab; die Engine
    # kennt nur den kontinuierlichen Tilt. Kein Hit@3-Gate: der Regler will
    # gerade vom High-Elo-Kauf abweichen ("Regel schlaegt Metrik"). Wirkung
    # und Kalibrierkonstanten: engine/rec_style.py.
    style_tilt: float = 0.0
    # Staerke der Verschiebung: `faktor = clamp(1 + tilt * achse * style_scale,
    # style_floor, 2 - style_floor)` auf dem BASISTERM der Pool-Scores
    # (engine/rec_style.py). Wert 0.95 aus der Kalibrierung an den 16.17-Zahlen
    # von Gwen JUNGLE (plan_spielstil.md Abschnitt 6) - er ist der einzige
    # Bereich, in dem alle drei Abnahme-Faelle gleichzeitig halten:
    #   Stufe 1 (Tilt -1): Riftmaker gewinnt Slot 3 vor Shadowflame und Zhonya
    #     Slot 4 vor Rabadon (der Floor deckelt die Schadens-Daempfung dort);
    #   Stufe 2 (Tilt -0.75): Riftmaker gewinnt Slot 3 noch, Slot 4 aber wieder
    #     Rabadon - erst dadurch unterscheidet sich Stufe 2 von Stufe 1;
    #   Stufe 5 (Tilt +1): Dusk and Dawn (73 % Pick, Achse 0) bleibt Erstkauf.
    # Unter ~0.94 kippt Slot 3 auf Stufe 2 zurueck auf Shadowflame, ueber ~0.97
    # faellt Stufe 2 mit Stufe 1 zusammen.
    style_scale: float = 0.95
    # Untergrenze des Faktors (und, gespiegelt, Obergrenze 2 - style_floor).
    # KEIN Rausch-Filter, sondern eine Reihenfolge-Garantie: bei voller
    # Daempfung auf 0 laegen alle zurueckgestuften Items gleichauf, und die
    # gelernte Reihenfolge UNTER ihnen ginge verloren - der Regler soll
    # umsortieren, nicht Daten loeschen.
    style_floor: float = 0.2
    # Boots-Prior des Reglers: |style_tilt| * style_boots_cap, Richtung nach
    # Vorzeichen (Resistenz-Boots bei Tilt < 0, Offensiv-Boots zum eigenen
    # Schadenstyp bei Tilt > 0, s. rec_boots._style_boots_prior). Bewusst NICHT
    # unter `boots_prior_cap` gedeckelt: der Deckel dort schuetzt die
    # Champion-Statistik vor COMP-Signalen (Janna-Befund, V2-03), hier gibt der
    # Spieler die Richtung selbst vor. 0.5 ist an der SCHWAECHSTEN Position
    # kalibriert, die der Regler durchsetzen koennen muss: dem kanonischen
    # Offensiv-Boot-Fallback (F6), der per Konstruktion 0 % Pick-Rate hat und
    # trotzdem gegen eine Mehrheitswahl von ~42 % antritt (Realfall Briar
    # JUNGLE). Alles darunter laesst genau den Fall ins Leere laufen. Die halbe
    # Stufe 4 (0.75 * 0.5 = 0.375) bleibt dabei unter dieser Schwelle - dort
    # kippt die Wahl nur, wenn der Abstand der Pick-Raten ohnehin klein ist.
    style_boots_cap: float = 0.5
    # Klassen-Overlay auf Stufe 1 (plan_spielstil.md F3): Faktor, mit dem ein
    # gefilterter Klassen-Pool-Kandidat gegen die Champion-Evidenz antritt.
    # 0.5 heisst "darf mitspielen, gewinnt aber nur mit deutlichem Abstand" -
    # die Regel "Champion-Evidenz vor Klassen-Daten" gilt damit weiter ueber den
    # Faktor statt ueber den harten Ausschluss. 0.0 schaltet das Overlay
    # komplett ab (Ablation).
    style_class_factor: float = 0.5
    # Ab welchem Tilt das Klassen-Overlay ueberhaupt greift (Tilt <= Schwelle).
    # -1.0 = nur Regler-Stufe 1. Bewusst eine Schwelle und kein Bool: der Sweep
    # kann so auch "ab Stufe 2" messen, ohne dass es einen zweiten Schalter
    # braucht.
    style_class_tilt: float = -1.0


DEFAULT_WEIGHTS = Weights()


# Abbildung der fuenf Regler-Stufen (Frontend/Server) auf `Weights.style_tilt`.
# Stufe 3 ist der Default jedes Spiels. Die Mittelstufen sind KALIBRIER-
# konstanten (plan_spielstil.md, Abschnitt 6): bei linearem +-0.5 reicht der
# Tilt auf den 16.17-Zahlen von Gwen JUNGLE nicht, um Stufe 2 von Stufe 3 zu
# unterscheiden (Shadowflame bliebe vor Riftmaker). Mit +-0.75 und
# `style_scale = 0.95` liegen die drei defensiven Stufen sauber auseinander:
# Stufe 3 Shadowflame, Stufe 2 Riftmaker dann Rabadon, Stufe 1 Riftmaker dann
# Zhonya. Hier liegt die EINE Definition, damit Server, Backtest und Tests
# dieselbe Abbildung nutzen.
PLAYSTYLE_DEFAULT = 3
PLAYSTYLE_TILT = {1: -1.0, 2: -0.75, 3: 0.0, 4: 0.75, 5: 1.0}


def tilt_for_level(level: int) -> float:
    """Regler-Stufe (1..5) -> `style_tilt`. Unbekannte Stufe -> neutral (0.0),
    damit ein kaputter Wert die Empfehlung nie verschiebt."""
    try:
        return PLAYSTYLE_TILT.get(int(level), 0.0)
    except (TypeError, ValueError):
        return 0.0


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
