"""Empfehlungs-Engine: Wissensbasis + Gegner-Profile + eigene Performance -> Items.

Stance-Logik:
  struggling (schlechte KDA / viele Tode)  -> defensiv
  ahead (gute KDA und Gold-Vorsprung)      -> aggressiv
  sonst                                    -> ausgewogen
"""

from dataclasses import dataclass, field, replace

from core import stats
from . import champions, items, knowledge, profiling
# Fassade (Struktur-Review 2026-07-17 T2): die Themen-Helfer liegen jetzt in
# eigenen Modulen, werden hier aber unter ihren alten Namen re-exportiert. Tests
# und pipeline/backtest.py greifen massiv auf `recommend.<name>` zu (auch auf die
# Unterstrich-Namen und Konstanten) - darum das noqa: F401.
from .rec_stance import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    STATE_LEAD_GOLD,
    fielded_lead, earned_lead, lead_note, own_stance, _stance_note,
)
from .rec_archetype import _select_archetype  # noqa: F401  Fassade (T2)
from .rec_explain import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _bucket_label, explain_item, _item_tag, _is_defensive, tag_fields,
)
from .rec_antiheal import (  # noqa: F401  Fassade, Struktur-Review 2026-07-17 T2
    _my_damage_type, _antiheal_recommendation,
)


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
    bleibt: `own_stance`, `stance`/`stance_reason`/`stance_note` (Badge/Text im
    Frontend) und der Archetyp-Tilt (`_select_archetype`) sind unveraendert -
    sie greift nur nicht mehr ins Ranking oder in die Kaufreihenfolge ein."""
    threat_cap: float = 0.2           # Cap des by_threat-Schubs (+/-)
    threat_scale: float = 0.8         # Skalierung des by_threat-Schubs
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


DEFAULT_WEIGHTS = Weights()


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


def _enemy_damage_bucket(enemy_profiles: list[dict]) -> str | None:
    """'ad' | 'ap' | None - Schadenstyp-Bucket des Gegnerteams in der
    TRAIN-Definition (ungewichtetes Mittel der Champion-Priors, Gegner ohne
    Prior werden ausgelassen; s. pipeline/aggregate.py `_dmg_bucket`).

    Bewusst NICHT der threat-gewichtete `team_damage_split`: die by_threat-/
    boots_by_threat-Zellen wurden unter dieser Definition GEZAEHLT, und Train und
    Serve muessen denselben Bucket bilden (Review-Befund G). `team_damage_split`
    bleibt fuer Boots-Regeltexte, `_is_defensive` und die UI zustaendig."""
    shares = [s for s in (champions.ad_share_for_id(p.get("champion_id"))
                          for p in enemy_profiles) if s is not None]
    bucket = champions.damage_bucket(shares)
    return bucket if bucket in ("ad", "ap") else None


def _spike_warnings(enemy_profiles: list[dict], my_completed: int) -> list[dict]:
    """Awareness-Regel ohne jede Champion-Statistik (Review Befund 4.2): hat ein
    Gegner >= 1 fertiges Item mehr als der Spieler, eine Spike-Warnung erzeugen
    (>= 2 dringlicher). Reine Information - KEIN Eingriff ins Scoring/die Stance."""
    out = []
    for e in enemy_profiles:
        ec = e.get("completed_items")
        if ec is None:
            continue
        ahead = ec - my_completed
        if ahead < 1:
            continue
        plural = "Items" if ahead > 1 else "Item"
        out.append({
            "name": e.get("name"),
            "enemy_items": ec,
            "my_items": my_completed,
            "ahead_by": ahead,
            "urgency": "high" if ahead >= 2 else "medium",
            "message": f"{e.get('name')} ist dir {ahead} {plural} voraus",
        })
    out.sort(key=lambda w: -w["ahead_by"])
    # Deckel (Review Befund H.1): liegt man hinten, wuerden sonst alle fuenf
    # Gegner gleichzeitig warnen (Alarm-Tapete). Nur die 2 groessten Vorspruenge.
    return out[:2]


def confidence_tier(kb: dict) -> str:
    """'rich' | 'basic' | 'thin' aus der Datenlage der Kombi.
    thin  = gar kein KB-Eintrag (unter cfg.min_games kein Build-Wissen),
    basic = Eintrag vorhanden, aber unter CONF_RICH_MIN (situative Schichten
            zu duenn - nur der Core-Pfad ist belastbar),
    rich  = >= CONF_RICH_MIN Spiele, volles Verhalten."""
    if not kb:
        return "thin"
    return "rich" if kb.get("games", 0) >= CONF_RICH_MIN else "basic"


def _shrunk(win_rate: float, n: int, base: float, k: float = SHRINK_K) -> float:
    """Geschrumpfte Win-Rate Richtung Basisrate. Duenner Wrapper um
    core.stats.shrunk (Befund D3) mit SHRINK_K als Item-Prior-Default."""
    return stats.shrunk(win_rate, n, base, k)


# Reine Sustain-Stats, bei denen ein zweites fertiges Item kaum Mehrwert bringt
# (anders als Ruestung/MR/HP, die Tanks bewusst stapeln - die NICHT abwerten).
_REDUNDANT_TAGS = {"LifeSteal", "SpellVamp"}


def _redundant_stack(name: str, owned_names: set[str]) -> bool:
    """True, wenn der Kandidat einen Sustain-Stat traegt, den ein bereits
    besessenes FERTIGES Item schon liefert (zweiter Lifesteal lohnt selten)."""
    cand = items.tags_of(name) & _REDUNDANT_TAGS
    if not cand:
        return False
    for owned in owned_names:
        oe = items.by_name().get(owned)
        if oe and not oe[1].get("into") and cand & set(oe[1].get("tags", [])):
            return True
    return False


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


def _synergy_boost(name: str, owned_ids: list[int], factor: float = 0.3) -> float:
    """0..factor - je weiter der Spieler ueber vorhandene Komponenten schon in
    dieses Item investiert hat, desto hoeher wird es priorisiert (nicht nur
    billiger). Belohnt 'fertigbauen, was ich angefangen habe'."""
    if not owned_ids:
        return 0.0
    entry = items.by_name().get(name)
    if not entry:
        return 0.0
    total = entry[1].get("gold", {}).get("total", 0) or 1
    disc = items.build_discount(name, owned_ids)
    return factor * min(disc / total, 1.0)


@dataclass
class _RecContext:
    """Langlebige Zwischenergebnisse einer recommend()-Auswertung (Struktur-
    Review 2026-07-17 T3, Befund S1). Buendelt Kontext- und Kandidaten-Daten, die
    ueber mehrere Phasen (Core-Pick, Boots, konditionale Schichten, Scoring,
    Result-Assembly) hinweg leben, statt sie als lange Argumentketten
    durchzureichen. `_build_context` baut das Objekt auf; `_conditional_layers`
    befuellt die konditionalen/Klassen-Felder nach."""
    # Rohe Aufruf-Parameter, die spaetere Phasen noch brauchen
    champion: str
    used_role: str | None
    role: str | None
    cid: str
    owned_names: set
    owned_ids: list
    enemy_profiles: list
    ally_items: set
    game_time: float
    current_gold: int | None
    weights: Weights
    # Botlane-Partner-Kontext (research_bot_sup_mates.md 9.40): das Profil-Dict
    # des eigenen BOTTOM-Partners plus Schluessel `partner_class`, oder None fuer
    # alle Nicht-UTILITY-Faelle. In dieser Tranche nur abgelegt (Durchreichung);
    # der auswertende Layer folgt in T4.
    bot_partner: dict | None
    # Wissensbasis + abgeleitete Kontext-Kennzahlen (Phase 1)
    kb: dict
    top: dict | None
    split: dict
    enemy_cc_score: float
    fielded_lead: int | None
    earned_lead: int | None
    gold_state: str | None
    stance: str
    stance_reason: str
    build: dict
    build_reason: str
    core_source: list
    situational_source: list
    confidence: str
    has_boots: bool
    boots_options: list
    # Uebergangs-Bigramm (next_after, T1/T2): {<Vorgaenger>: [{item, count,
    # win_rate}]} des Champion+Rollen-Eintrags. Leer bei alter KB ohne Block.
    next_after: dict = field(default_factory=dict)
    # Ausgewertetes Bigramm (einmal pro Lauf gebaut, s. _next_after_model) plus
    # die FERTIGEN Besitz-Items als Bezugsmenge O des Lifts.
    na_cond: dict = field(default_factory=dict)
    na_marginal: dict = field(default_factory=dict)
    na_owned: list = field(default_factory=list)
    # Restpfad-Neubewertung (V2-05): Slot-Support P(Slot|Item), aktueller
    # Kaufslot, gelernte Exklusiv-Paare. `path_scores`/`path_block` werden von
    # _core_pick/_score_situationals BEFUELLT (Pool-Score je Kandidat bzw. Namen
    # ohne Support am aktuellen Slot) und in _assemble_result gelesen.
    slot_dist: dict = field(default_factory=dict)
    slot_horizon: int = 0
    cur_slot: int = 1
    exclusive: list = field(default_factory=list)
    path_scores: dict = field(default_factory=dict)
    path_block: frozenset = frozenset()
    # Todes-Signal aus dem Live-Kill-Feed (V2-08, `engine/rec_deaths.py`):
    # {deaths, champion, champion_deaths, damage_type, trigger, reason} oder
    # None. Default None (Layer stumm) - so bleiben alte Aufrufer/Tests, die den
    # Kontext direkt bauen, unveraendert gueltig, und Backtest/Demo/alte Dumps
    # verhalten sich exakt wie vor V2-08.
    death_signal: dict | None = None
    # Konditionale Schichten (by_threat/by_state) - von _conditional_layers gefuellt
    enemy_bucket: str | None = None
    bt: dict | None = None
    threat_items: dict = field(default_factory=dict)
    threat_base: float | None = None
    state_items: dict = field(default_factory=dict)
    state_base: float | None = None
    # Partner-konditioniert (by_partner, Phase 2) - von _conditional_layers gefuellt
    partner_items: dict = field(default_factory=dict)
    partner_base: float | None = None
    partner_bucket: str | None = None
    # Klassen-Fallback - von _conditional_layers gefuellt
    lookup_role: str = ""
    class_bucket: str | None = None
    class_situational: list = field(default_factory=list)
    class_boots: list = field(default_factory=list)
    class_games: int = 0
    class_label: str = ""
    champ_pool: set = field(default_factory=set)


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


def _tag_role(ctx: _RecContext) -> str | None:
    """Rolle fuer das Support-Framing der Tags/Erklaertexte (T4b): nur "UTILITY"
    aktiviert die Support-Kategorien, sonst None (byte-identisches Verhalten)."""
    return ctx.role if ctx.role == "UTILITY" else None


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
    `min_n` Kaeufen - passende Resistenz zuerst, dann die haeufigere Zelle."""
    rows = [r for r in (cell or {}).get("items") or []
            if r.get("defensive") and int(r.get("count") or 0) >= min_n
            and _def_usable(ctx, r.get("item", ""), taken)]
    if not rows:
        return None
    rows.sort(key=lambda r: (_def_rank(r["item"], want), -int(r.get("count") or 0)))
    return rows[0]


def _pool_def_row(ctx: _RecContext, taken: set[str],
                  want: str | None) -> dict | None:
    """Letzte Stufe: defensiv getaggtes Item aus dem Champion-Pool selbst
    (core + situational). Ohne Behind-Statistik, dafuer mit den globalen Zahlen
    des Champions - und genau so wird es im Text auch ausgewiesen."""
    rows = [e for e in list(ctx.situational_source) + list(ctx.core_source)
            if items.tags_of(e.get("item", "")) & items.DEF_TAGS
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
