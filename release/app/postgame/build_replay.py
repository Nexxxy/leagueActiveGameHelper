"""Build-Eval Stufe 3: Engine-Replay (Phase 5, s. plan_postgame.md §6/§8b).

Fuer jeden der fuenf Team-Spieler wird an JEDEM Fertig-Item-Zeitpunkt der
Spielzustand UNMITTELBAR VOR dem Kauf rekonstruiert und in die projekteigene
Empfehlungs-Engine (`engine.recommend.recommend`) gefuettert - genau der Live-Code-
Pfad. War das tatsaechlich gekaufte Item in den Top-3 der Engine-Empfehlung? Das
ergibt einen Build-Score je Spieler ("X von Y Kaeufen engine-konform") plus die
Abweichungen mit der Engine-Alternative. Das ist das Alleinstellungsmerkmal des
Projekts und funktioniert **key-frei in allen Pfaden**.

Zustands-Rekonstruktion (key-frei, quellenunabhaengig): beide Report-Pfade tragen
je Spieler eine `items_ts`-Serie (je-Minute gehaltene Item-IDs) - im Timeline-Pfad
aus dem Inventar-Replay (`series._inventory_ids`), im Dump/Capture-Pfad aus der
Live-Item-Liste (`live_series`). Daraus + den Minuten-Serien (level/cs) + dem
Kill-Event-Strom (KDA) wird der Vor-Kauf-Zustand aller 10 gebaut.

**Geteilte Adapter aus `engine/replay_profile.py`** (urspruenglich in
`pipeline/backtest.py`, seit dem Release-Entkopplungs-Fix ausgelagert, damit das
App-Release nicht an die Crawl-Pipeline gekoppelt ist): `enemy_profile`
(Live-API-foermiges, threat-bewertetes Gegner-Profil aus einem Minuten-Snapshot)
und `replay_candidates` (deterministische Kandidatenliste [next] + situationals
aus einem recommend()-Ergebnis) - dieselbe Logik, die der Offline-Backtest nutzt;
der Bot-Partner-Kontext (UTILITY) spiegelt `backtest._make_sample`.

**Top-3-Definition:** identisch zum Backtest
(`replay_candidates(result, exclude_boots=True)[:3]` = `[next]` +
`result["items"]`, dedupliziert, OHNE Boots-Eintraege). Ein regulaerer
Fertig-Kauf ist ein "Hit", wenn er in diesen Top-3 liegt. Der Boots-Filter ist
der Fix zu Befund 1 (plan_engine_v2.md): Boots belegten sonst zwei der drei
Slots und drueckten echte Core-Items aus der Wertung.

**Drei Stufen statt Hit/Miss (V2-06, plan_engine_v2.md Konzept 2):** Seit der
Restpfad-Neubewertung (V2-05) folgt die Engine dem tatsaechlich gebauten Pfad -
ein Kauf ausserhalb der Top-3 ist damit nicht automatisch falsch, sondern oft
nur "die zweite uebliche Variante". Jeder regulaere Item-Kauf bekommt darum eine
`grade`:

  "hit"  (konform)     - in den Top-3 des pfad-bewussten recommend-Ergebnisses.
  "ok"   (vertretbar)  - nicht Top-3, aber mit eigenem Rueckhalt: Core-Item der
                         Kombi, frueherer Engine-Vorschlag oder KB-Statistik
                         ("statistisches Medium") - Rangfolge s. unten.
  "miss" (Abweichung)  - weder noch.

Der **Troll-Guard** ist der Kern der Stufe "vertretbar": sie wird NICHT aus der
Neubewertung abgeleitet (die wuerde jeden Fantasie-Build absegnen), sondern
verlangt eine eigene, konditionale KB-Evidenz FUER DIESEN Champion+Rolle -
Slot-Support, `next_after`-Anteil oder eine `by_state`-Zelle, jeweils ueber
kalibrierten Schwellen und konfliktfrei gegen den Besitz. Ein Item ohne jede
solche Evidenz bleibt Abweichung.

**Rueckhalt-Quellen in fester Rangfolge.** Ein empirischer Review ueber zehn
echte Matches hat gezeigt, dass die urspruenglichen drei KB-Quellen die
REIHENFOLGE mitbewerten, obwohl nur die Item-WAHL gemessen werden soll. Darum
sind zwei Quellen vorgeschaltet:

  0. `_blocked` - Kollision mit dem Besitz (geteilte Passive oder gelerntes
     Exklusiv-Paar). Vorrangig vor allem anderen: so ein Kauf bleibt Abweichung,
     auch wenn ihn jede andere Quelle tragen wuerde.
  1. **Core-Item** (`_core_names`) - das Item steht im Core der Kombi: im
     globalen Core des KB-Eintrags ODER im Core irgendeines Archetyps
     (`builds`), denn wer auf Archetyp 2 spielt, baut dessen Core. Realfall:
     Varus TOP kaufte sein Core-Item #2 ("Dusk and Dawn") als ERSTES - die
     Engine will an Slot 1 Core #1, also war der Kauf "miss", obwohl er
     buchstaeblich zum Build gehoert. Abweichend ist nur die Reihenfolge.
  2. **Top-3-Gedaechtnis** - das Item stand bei einem FRUEHEREN Kauf desselben
     Spielers selbst in den Engine-Top-3. Realfall: Hwei BOTTOM - Shadowflame
     lag bei Min 24/32/40 in der Empfehlung; der Kauf bei Min 46 wurde "miss",
     weil ein Off-KB-Zwischenkauf (Jak'Sho) die `next_after`-Kette riss und
     `slot_dist` fuer spaete Slots geprunt ist. Was die Engine selbst wollte,
     ist nicht dadurch falsch, dass es spaeter kam.
  3.-5. die kalibrierten KB-Quellen aus `_ok_reason` (Slot-Support,
     `next_after`, `by_state`) - unveraendert der harte Troll-Guard.

Der `next_after`-Check nimmt dabei nicht stur das zuletzt gekaufte Item als
Vorgaenger, sondern den JUENGSTEN Vorgaenger, den die KB ueberhaupt kennt
(Backoff ueber die Kaufhistorie). Sonst reisst ein einziger Off-KB-Zwischenkauf
den Uebergangs-Rueckhalt fuer alle Folgekaeufe ab, obwohl die KB fuer den
vorletzten Kauf Daten haette.

`score.hits` bleibt exakt "Anzahl Kaeufe in Top-3" (damit persistierte
Trend-Records vergleichbar bleiben) - die Quellen 1 und 2 erhoehen nur
`score.ok`.

**Boots:** die Engine hat eine eigene Boots-Logik (CC-lastiges Team -> Tenacity,
sonst AD/AP-Konter). Boots-Kaeufe werden mitbewertet, aber in einer EIGENEN
Teilmenge gezaehlt (gegen die Boots-Kandidaten der Engine, nicht die allgemeinen
Top-3) - so entscheidet ein Mercs-vs-Steelcaps-Streit nicht ueber den
Item-Score. Zwei Korrekturen aus demselben Review:

  * Gibt die Engine im Vor-Kauf-Zustand GAR KEINE Boots-Empfehlung ab, wird der
    Kauf nicht gewertet (kein `btotal`-Inkrement, kein purchases-Eintrag) -
    analog zum Item-Fall "Engine hat nichts zu sagen". Genau das passiert beim
    ZWEITEN T2-Boots-Paar (Verkauf/Umbau; real: Renekton Mercs Min 12 ->
    Steelcaps Min 27): wer schon Boots traegt, bekommt keine vorgeschlagen - der
    Kauf waere sonst automatisch "miss" gegen eine LEERE Alternative
    ("-> Engine: -"), also gegen gar keine Aussage.
  * Gemessen wird gegen "die ueblichen Boots laut Statistik", nicht gegen die
    Engine-Top-1: ein Boots-Kauf ist auch dann konform, wenn er in den KB-Boots
    der Kombi mit `pick_rate >= BOOTS_OK_PICK_MIN` steht. Die Engine fieldet oft
    nur ein bis zwei Boots-Eintraege (Hauptvorschlag + Alternative); real fiel
    damit Renektons Mercury's Treads durch, obwohl 30 % der Renektons sie bauen.
    Ohne Boots-Block in der KB bleibt es bei der reinen Engine-Liste
    (Code-Fallback bei duenner Datenlage). Die Anzeige `engine_top` ist
    unveraendert die Engine-Liste - sie zeigt, was die Engine gesagt haette.

Kein KB-Wissen fuer Champion+Rolle UND kein Klassen-Fallback -> der Spieler wird
sauber als "nicht bewertbar" markiert (kein Raten). Fehlt der Data-Dragon-Static-
Cache/builds.yaml (Schicht 0), faellt jeder Kauf-Loop auf eine leere Auswertung
zurueck (der Aufrufer kapselt zusaetzlich mit try/except) - kein Crash.
"""

from engine import champions, items as app_items, knowledge, profiling, rec_partner
from engine import recommend as rec

from engine.replay_profile import enemy_profile, replay_candidates

# "Fertig"-Schwelle wie Stufe 1+2 (analysis.FINISHED_GOLD): ein Item gilt als
# fertig, wenn es im builds.yaml-Core steht ODER sein Gesamt-Gold >= 2000 ist.
FINISHED_GOLD = 2000


# --- Item-Klassifikation ----------------------------------------------------

def _item_total_gold(name: str) -> int:
    """Gesamt-Gold eines Items ueber den Data-Dragon-Static-Cache (engine.items)."""
    entry = app_items.by_name().get(name)
    return entry[1].get("gold", {}).get("total", 0) if entry else 0


def _classify(iid: int, core_names) -> tuple[str, str] | None:
    """Klassifiziert einen Kauf: ('boots', name) | ('item', name) | None.

    'boots' = echtes Boots-Upgrade (T2+, `is_upgraded_boots`) - eigene Teilmenge.
    'item'  = Fertig-Item (Core-Zugehoerigkeit ODER Gesamt-Gold >= FINISHED_GOLD).
    None    = weder noch (Komponenten, Consumables/Elixiere/Trinkets, Basis-Boots)
              -> wird nicht bewertet."""
    name = app_items.name_of(iid)
    if not name:
        return None
    if app_items.is_upgraded_boots(name):
        return ("boots", name)
    if name in core_names or _item_total_gold(name) >= FINISHED_GOLD:
        return ("item", name)
    return None


# --- Zustands-Rekonstruktion ------------------------------------------------

def _at(seq, idx, default=0):
    """seq[idx] mit Clamp auf [0, len-1]; leere Serie -> default."""
    if not seq:
        return default
    if idx < 0:
        idx = 0
    elif idx >= len(seq):
        idx = len(seq) - 1
    return seq[idx]


def _cum_kda(kills: list, pid, upto_minute: float) -> dict:
    """Kumulative KDA eines Spielers bis (inkl.) `upto_minute` aus dem Kill-Strom.
    Key-frei in beiden Pfaden (der Kill-Event-Strom liegt ueberall vor)."""
    k = d = a = 0
    for e in kills:
        if e.get("minute", 0.0) > upto_minute:
            continue
        if e.get("killer") == pid:
            k += 1
        if e.get("victim") == pid:
            d += 1
        if pid in (e.get("assists") or []):
            a += 1
    return {"kills": k, "deaths": d, "assists": a}


def _snapshot_dicts(ser: dict, ranked_names: dict, pids, p_idx: int,
                    minute: float) -> tuple[dict, dict, dict, dict, dict]:
    """Baut die Minuten-Snapshot-Dicts (inv/kda/cs/level/pid_meta) fuer `pids` am
    Serien-Index `p_idx` - genau die Form, die `replay_profile.enemy_profile`
    erwartet. So wird die Gegner-Profil-Konstruktion des Offline-Backtests
    unveraendert wiederverwendet."""
    players = ser["players"]
    kills = ser["events"]["kills"]
    inv, kda, cs, level, pid_meta = {}, {}, {}, {}, {}
    for q in pids:
        s = players.get(q, {})
        inv[q] = list(_at(s.get("items_ts", []), p_idx, []) or [])
        kda[q] = _cum_kda(kills, q, minute)
        cs[q] = _at(s.get("cs", []), p_idx, 0)
        level[q] = _at(s.get("level", []), p_idx, 0)
        info = ranked_names.get(q, {})
        pid_meta[q] = (info.get("team"), info.get("role") or "",
                       info.get("champ") or "", False)
    return inv, kda, cs, level, pid_meta


def _bot_partner(ser: dict, ranked_names: dict, pid, p_idx: int,
                 minute: float) -> dict | None:
    """Bot-Partner-Kontext fuer UTILITY (spiegelt backtest._make_sample): das
    Profil des eigenen BOTTOM-Allys + `partner_class`. None fuer alle anderen."""
    info = ranked_names.get(pid, {})
    team = info.get("team")
    for q, qinfo in ranked_names.items():
        if q == pid or qinfo.get("team") != team or qinfo.get("role") != "BOTTOM":
            continue
        s = ser["players"].get(q, {})
        kda = _cum_kda(ser["events"]["kills"], q, minute)
        partner_player = {
            "championName": qinfo.get("champ") or "",
            "level": _at(s.get("level", []), p_idx, 0),
            "scores": {"kills": kda["kills"], "deaths": kda["deaths"],
                       "assists": kda["assists"],
                       "creepScore": _at(s.get("cs", []), p_idx, 0)},
            "items": [{"itemID": i} for i in _at(s.get("items_ts", []), p_idx, []) or []],
        }
        prof = profiling.profile_player(partner_player)
        pclass = rec_partner.classify_partner(prof["champion_id"], prof)
        return {**prof, "partner_class": pclass}
    return None


# --- KB-Gate ----------------------------------------------------------------

def _has_kb(cid: str, role: str) -> bool:
    """True, wenn die Engine ueberhaupt Wissen fuer diese Kombi hat: ein
    Champion+Rolle-Eintrag ODER (Fallback) ein Klassen-Eintrag fuer den Bucket.
    Beides leer -> "nicht bewertbar" (kein Raten)."""
    _used_role, kb = knowledge.for_champion(cid, role)
    if kb:
        return True
    bucket = champions.bucket_for_id(cid)
    return bool(bucket and knowledge.for_class(bucket, role))


# --- Stufe "vertretbar": statistischer Rueckhalt (V2-06, Troll-Guard) --------
#
# Schwellen KALIBRIERT, nicht geraten (plan_engine_v2.md §7). Messung am
# 16.15-Cache mit der Backtest-Trainings-KB (data/backtest/16.15/train/
# builds.yaml): 100 Holdout-Matches -> 2.851 echte Item-Kaeufe (Boots
# ausgenommen). Jeder Zustand wurde dreimal bewertet - mit dem ECHTEN Kauf, mit
# einem ROLLENFREMDEN Item (Enchanter-Items auf Carrys, Crit-ADC-Items auf
# Supports) und mit einem ZUFAELLIGEN fertigen SR-Item (120er-Pool, der harte
# Troll-Guard-Test "waere jeder Fantasie-Build vertretbar?").
#
#   "akzeptiert" = konform + vertretbar        echt   rollenfremd   zufaellig
#     Slot 0.05 / NA 0.05 / State n>=5        84.6 %      0.1 %       4.2 %
#     Slot 0.08 / NA 0.08 / State n>=8        83.8 %      0.1 %       4.0 %
#     Slot 0.10 / NA 0.10 / State n>=10       83.7 %      0.1 %       3.7 %  <-
#     Slot 0.12 / NA 0.12 / State n>=12       82.9 %      0.1 %       3.5 %
#     Slot 0.15 / NA 0.15 / State n>=20       81.1 %      0.1 %       3.3 %
#     Slot 0.25 / NA 0.25 / State n>=30       78.5 %      0.1 %       3.1 %
#   (konform allein liegt konstant bei 67.2 % echt / 2.0 % zufaellig - die
#    Top-3-Stufe haengt nicht an diesen Schwellen, nur die mittlere Stufe.)
#
# Gewaehlt ist 0.10 / 0.10 / 10: der Knick der Kurve. Bis dahin kostet jede
# Verschaerfung fast nichts an echten Kaeufen (84.6 -> 83.7 %) und drueckt die
# Zufalls-Akzeptanz spuerbar (4.2 -> 3.7 %); danach dreht sich das Verhaeltnis
# (0.15 kostet 2.6 Punkte echte Kaeufe fuer 0.4 Punkte weniger Zufall). Beide
# Zielbilder sind erfuellt: echte Kaeufe landen ueberwiegend, aber NICHT
# vollstaendig in konform+vertretbar (16 % bleiben Abweichung - der Check misst
# also noch etwas), waehrend Fantasie-Kaeufe zu 96 % Abweichung bleiben.
# Verteilung der Rueckhalt-Quellen bei den vertretbaren echten Kaeufen:
# Slot-Support 76 %, by_state 16 %, next_after 8 % - alle drei tragen.
# Mess-Skript: tmp/calib_v2_06_grades.py (ausserhalb des Repos).

# Mindest-Anteil von P(Slot | Item) am AKTUELLEN Kaufslot. `slot_dist` ist
# pipeline-seitig bereits bei n < SLOT_DIST_MIN_N (=5) gepruned - ein
# ausgewiesener Slot hat also immer ausreichend Beobachtungen; die Schwelle
# filtert die Rest-Slots ("bauen ein paar, ist aber nicht der Slot dafuer").
OK_SLOT_SHARE_MIN = 0.10

# Mindest-Anteil P(Kauf | frueher fertiges Item) im `next_after`-Bigramm. Die
# Uebergaenge sind bei count < MIN_NEXT_AFTER (=10) geprunt - auch hier traegt
# die Pipeline das Mindest-n, die Schwelle filtert die Ausreisser-Nachfolger.
# Der Vorgaenger ist nicht zwingend der letzte Kauf, s. `_na_predecessor`.
OK_NEXT_AFTER_MIN = 0.10

# Mindest-`count` in der passenden `by_state`-Zelle (ahead/behind). Bewusst ein
# absolutes n statt eines Anteils: die Zellen sind je Champion unterschiedlich
# gross, und der Cutoff der Pipeline (MIN_STATE_ITEM = 5) ist fuer eine
# "vertretbar"-Aussage zu weich.
OK_STATE_MIN_N = 10

# Mindest-`pick_rate` eines KB-Boots-Eintrags, ab dem der Kauf als konform
# zaehlt - auch wenn die Engine gerade ein anderes Paar vorschlaegt. Die Messlatte
# der Boots-Quote ist damit "die ueblichen Boots dieser Kombi laut Statistik" und
# nicht "die Engine-Top-1": die Boots-Empfehlung fieldet oft nur ein bis zwei
# Eintraege, und ein Mercs-vs-Steelcaps-Streit ist keine Fehlentscheidung des
# Spielers. Der Wert liegt bewusst auf derselben 10-%-Linie wie
# OK_SLOT_SHARE_MIN/OK_NEXT_AFTER_MIN: dort trennt die Verteilung "bauen ein
# paar" von "ist eine der ueblichen Varianten".
BOOTS_OK_PICK_MIN = 0.10


def _core_names(cid: str, role: str) -> set[str]:
    """Alle Core-Item-Namen der Kombi laut KB (Rueckhalt-Quelle 1).

    Vereinigung aus dem globalen `core` des Eintrags UND dem `core` JEDES
    Archetyps in `builds`: wer auf Archetyp 2 spielt, baut dessen Core, und die
    Bewertung darf ihn nicht gegen den Standard-Core messen. Leere Menge, wenn
    die Kombi unbekannt ist - dann kann die Quelle schlicht nie greifen."""
    _used_role, kb = knowledge.for_champion(cid, role)
    blocks = [kb.get("core") or []]
    blocks += [(b.get("core") or []) for b in (kb.get("builds") or [])
               if isinstance(b, dict)]
    out: set[str] = set()
    for block in blocks:
        for it in block or []:
            name = it.get("item") if isinstance(it, dict) else it
            if name:
                out.add(str(name))
    return out


def _kb_boots(cid: str, role: str) -> dict[str, float]:
    """Boots-Name -> `pick_rate` aus dem `boots`-Block der Kombi.

    Grundlage der Boots-Wertung gegen "die ueblichen Boots" statt gegen die
    Engine-Top-1. Leeres Dict fuer unbekannte Kombis und fuer KBs/Fixtures ohne
    Boots-Block - dann faellt die Wertung auf die reine Engine-Liste zurueck."""
    _used_role, kb = knowledge.for_champion(cid, role)
    out: dict[str, float] = {}
    for b in kb.get("boots") or []:
        if not isinstance(b, dict) or not b.get("item"):
            continue
        try:
            out[str(b["item"])] = float(b.get("pick_rate") or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def _support_kb(cid: str, role: str) -> dict:
    """KB-Rueckhalt-Daten EINER Kombi, einmal je Spieler gelesen:

        {"slot": {Item: {Slot: Anteil}},          (V2-04 `slot_dist`)
         "na":   {Vorgaenger: {Item: Anteil}},    (T1 `next_after`, count-normiert)
         "state": {ahead|behind: {Item: count}},  (V2-07 `by_state`)
         "exclusive": [frozenset(2 Namen), ...]}  (V2-04 `exclusive`)

    Alle vier Bloecke sind additiv: eine KB vor V2-04/V2-07 (oder eine
    Test-Fixture) liefert leere Dicts - dann kann `_ok_reason` nie greifen und
    die Bewertung faellt sauber auf das alte Zweistufen-Verhalten zurueck."""
    used_role, kb = knowledge.for_champion(cid, role)
    na: dict[str, dict[str, float]] = {}
    for prev, succs in (kb.get("next_after") or {}).items():
        total = sum(s.get("count", 0) for s in succs or [])
        if total > 0:
            na[prev] = {s["item"]: s.get("count", 0) / total for s in succs}
    state: dict[str, dict[str, int]] = {}
    for st, cell in (kb.get("by_state") or {}).items():
        state[st] = {i["item"]: int(i.get("count", 0) or 0)
                     for i in (cell.get("items") or [])}
    return {"slot": knowledge.slot_dist(cid, used_role), "na": na,
            "state": state,
            "exclusive": knowledge.exclusive_pairs(cid, used_role)}


def _blocked(name: str, sup: dict, owned_names: set) -> bool:
    """Kollidiert der Kauf mit dem Besitz? Geteilte Passive (`items.conflicts`)
    ODER ein gelerntes Exklusiv-Paar (V2-04). Ein solcher Kauf ist NIE
    'vertretbar' - er ist der klarste Fall einer echten Abweichung."""
    if app_items.conflicts(name, owned_names):
        return True
    return any(name in pair and (pair - {name}) & owned_names
               for pair in sup.get("exclusive") or [])


def _na_predecessor(sup: dict, prev_items) -> str | None:
    """Der JUENGSTE bisher gekaufte Vorgaenger, den das `next_after`-Bigramm der
    Kombi ueberhaupt als Key kennt - oder None.

    Backoff statt "nur das zuletzt gekaufte Item": ein einziger Off-KB-Kauf
    (ein Item, das diese Kombi in der KB nie baut) hat sonst den
    Uebergangs-Rueckhalt fuer ALLE Folgekaeufe gekappt, obwohl die KB fuer den
    vorletzten Kauf sehr wohl Daten hat. Uebersprungen werden nur unbekannte
    Vorgaenger - der erste bekannte gewinnt, auch wenn er den konkreten
    Nachfolger nicht traegt (sonst wuerde die Suche so lange zurueckwandern, bis
    irgendein alter Kauf passt, und das waere kein Uebergangs-Beleg mehr)."""
    na = sup.get("na") or {}
    for prev in reversed(list(prev_items or [])):
        if prev in na:
            return prev
    return None


def _ok_reason(name: str, sup: dict, *, cur_slot: int, prev_items,
               gold_state: str | None, owned_names: set) -> str | None:
    """Grund-Text des statistischen Rueckhalts ("vertretbar") oder None.

    Drei gleichrangige Quellen, in dieser Reihenfolge geprueft (die erste, die
    traegt, liefert den Text):
      1. Slot-Support: so viele Spieler kaufen das Item genau an dieser Stelle.
      2. `next_after`: es folgt haeufig auf ein frueher fertiges Item
         (Vorgaenger-Wahl s. `_na_predecessor`).
      3. `by_state`: es wird in genau dieser Gold-Lage haeufig gekauft.
    Vorgeschaltet der harte Konflikt-Ausschluss (`_blocked`).

    `prev_items` ist die chronologische Liste der bisher gekauften regulaeren
    Fertig-Items (Boots zaehlen im Bigramm nicht mit)."""
    if _blocked(name, sup, owned_names):
        return None
    share = (sup.get("slot") or {}).get(name, {}).get(cur_slot)
    if share is not None and share >= OK_SLOT_SHARE_MIN:
        return f"Slot-üblich ({share:.0%} als {cur_slot}. Item)"
    prev_item = _na_predecessor(sup, prev_items)
    if prev_item:
        na = (sup.get("na") or {}).get(prev_item, {}).get(name)
        if na is not None and na >= OK_NEXT_AFTER_MIN:
            return f"folgt oft auf {prev_item} ({na:.0%})"
    if gold_state:
        cnt = (sup.get("state") or {}).get(gold_state, {}).get(name)
        if cnt is not None and cnt >= OK_STATE_MIN_N:
            # Der Rueckhalt-Text nennt die QUELLE zuerst ("Behind-Rueckhalt"),
            # damit im Report auf einen Blick lesbar ist, dass dieser Kauf
            # situativ begruendet war - und nicht nur "irgendwie statistisch
            # gestuetzt" (V2-08, Teil C).
            lage = "hinten" if gold_state == "behind" else "vorne"
            label = "Behind" if gold_state == "behind" else "Ahead"
            return f"{label}-Rückhalt: üblich, wenn du {lage} liegst (n={cnt})"
    return None


def _gold_state(owned_ids: list, used_role: str, enemies: list) -> str | None:
    """'ahead' | 'behind' | None - dieselbe Rechnung wie `recommend`
    (`fielded_lead` -> `earned_lead` -> STATE_LEAD_GOLD), damit die
    `by_state`-Zellen unter derselben Definition abgefragt werden, unter der sie
    gezaehlt wurden. `current_gold` ist im Replay unbekannt (None) - genau wie
    im Offline-Backtest."""
    my_gold = app_items.categorize_gold(owned_ids)["gold_total"]
    _lead, opp = rec.fielded_lead(my_gold, used_role, enemies)
    e_lead = rec.earned_lead(my_gold, None, opp)
    if e_lead is None:
        return None
    if e_lead >= rec.STATE_LEAD_GOLD:
        return "ahead"
    if e_lead <= -rec.STATE_LEAD_GOLD:
        return "behind"
    return None


# --- Haupt-Auswertung je Spieler --------------------------------------------

def evaluate_player(ser: dict, pid, ranked_names: dict, core_by_pid: dict,
                    *, weights=None) -> dict:
    """Engine-Replay-Auswertung fuer EINEN Team-Spieler.

    Rueckgabe bei bewertbarem Spieler:
      {evaluable: True, score:{hits,ok,total}, boots:{hits,total},
       purchases:[{minute,item,kind,hit,grade,ok_reason,engine_top:[...]}, ...]}
    Sonst: {evaluable: False, reason: "..."}.

    `score.hits` bleibt die Zahl der KONFORMEN Kaeufe (Top-3) - Semantik
    unveraendert gegenueber der Zweistufen-Version, damit persistierte Trend-
    Records vergleichbar bleiben. `score.ok` ist der Zaehler der vertretbaren
    Kaeufe (V2-06, Quellen s. Modul-Docstring); `total - hits - ok` sind die
    Abweichungen. `boots.total` zaehlt nur Boots-Kaeufe, zu denen die Engine
    ueberhaupt eine Empfehlung hatte.

    `core_by_pid`: {pid: [Core-Item-Namen]} (fuer die Fertig-Erkennung; auch die
    Gegner-Cores werden hier nur zur Item-Klassifikation gebraucht - der Engine-
    Input selbst kommt aus dem rekonstruierten Zustand). Nicht zu verwechseln
    mit `_core_names`: das liest den Core AUS DER KB und ist die
    Rueckhalt-Quelle 1 der Wertung."""
    info = ranked_names.get(pid, {})
    champ = info.get("champ") or ""
    role = info.get("role") or ""
    cid = champions.resolve_id(champ) or champ
    if not _has_kb(cid, role):
        return {"evaluable": False,
                "reason": "kein Build-Wissen (Champion+Rolle, auch keine "
                          "Klassen-Daten)"}

    w = weights or rec.DEFAULT_WEIGHTS
    core = core_by_pid.get(pid, [])
    players = ser["players"]
    kills = ser["events"]["kills"]
    items_ts = players.get(pid, {}).get("items_ts", []) or []

    my_team = info.get("team")
    other_team = 200 if my_team == 100 else 100
    enemy_pids = [q for q, qi in ranked_names.items() if qi.get("team") == other_team]
    ally_pids = [q for q, qi in ranked_names.items()
                 if qi.get("team") == my_team and q != pid]

    # KB-Rueckhalt der Kombi einmal lesen (Stufe "vertretbar", V2-06). Die
    # verwendete Rolle stammt aus derselben Aufloesung wie in `recommend`.
    used_role, _kb = knowledge.for_champion(cid, role)
    sup = _support_kb(cid, role)
    # Core-Namen (Rueckhalt-Quelle 1) und KB-Boots (Boots-Wertung) einmal je
    # Spieler lesen - beide sind ueber den ganzen Kauf-Loop konstant.
    kb_core = _core_names(cid, role)
    kb_boots = _kb_boots(cid, role)

    purchases = []
    hits = oks = total = bhits = btotal = 0
    # Chronologische Liste der bisher FERTIG gekauften regulaeren Items (Boots
    # zaehlen im `next_after`-Bigramm nicht mit, s. aggregate._next_after_pairs).
    # Der Uebergangs-Rueckhalt sucht darin den juengsten KB-bekannten Vorgaenger.
    prev_items: list[str] = []
    # Top-3-Gedaechtnis (Rueckhalt-Quelle 2): Item-Name -> letzte Minute, in der
    # die Engine es diesem Spieler selbst empfohlen hat.
    top3_seen: dict[str, int] = {}
    seen: set = set()
    for m, cur in enumerate(items_ts):
        for iid in (cur or []):
            if iid in seen:
                continue
            seen.add(iid)
            cls = _classify(iid, core)
            if cls is None:
                continue
            kind, name = cls
            # Vor-Kauf-Zustand = Minuten-Snapshot VOR der Fertigstellung (p_idx =
            # m-1). game_time = m*60 (Fertigstellungs-Minute). Naeherung wie im
            # Backtest (Minuten-Granularitaet).
            p_idx = m - 1 if m > 0 else 0
            minute = float(m)
            owned_ids = list(_at(items_ts, m - 1, []) or []) if m > 0 else []
            owned_names = {n for n in (app_items.name_of(i) for i in owned_ids) if n}
            my_kda = _cum_kda(kills, pid, minute)
            my_scores = {"kills": my_kda["kills"], "deaths": my_kda["deaths"],
                         "assists": my_kda["assists"],
                         "creepScore": _at(players.get(pid, {}).get("cs", []), p_idx, 0)}
            my_level = _at(players.get(pid, {}).get("level", []), p_idx, 0)
            # Gegner-Profile (threat-bewertet) - geteilter replay_profile-Adapter.
            inv, e_kda, cs, level, pid_meta = _snapshot_dicts(
                ser, ranked_names, enemy_pids, p_idx, minute)
            enemies = [enemy_profile(q, inv, e_kda, cs, level, pid_meta)
                       for q in enemy_pids]
            profiling.add_threat_scores(enemies, {})
            ally_items = {n for q in ally_pids
                          for n in (app_items.name_of(i)
                                    for i in (_at(players.get(q, {}).get("items_ts", []),
                                                  p_idx, []) or [])) if n}
            bot_partner = (_bot_partner(ser, ranked_names, pid, p_idx, minute)
                           if role == "UTILITY" else None)

            result = rec.recommend(
                champ, role, set(owned_names), dict(my_scores), enemies,
                game_time=minute * 60.0, current_gold=None,
                owned_ids=owned_ids, my_level=my_level,
                ally_items=set(ally_items), weights=w, bot_partner=bot_partner)

            # Item-Top-3 dieses Zustands - Bewertungsmassstab der regulaeren
            # Kaeufe UND Futter des Top-3-Gedaechtnisses (auch an
            # Boots-Zeitpunkten: was die Engine dort empfiehlt, hat sie diesem
            # Spieler gesagt, egal was er gerade kauft).
            item_top3 = replay_candidates(result, exclude_boots=True)[:3]

            reason = None
            if kind == "boots":
                # Boots gegen die EIGENE Boots-Logik der Engine messen (eigene
                # Teilmenge) - unabhaengig von den allgemeinen Top-3. Die drei
                # KB-Stufen gelten bewusst NUR fuer regulaere Item-Kaeufe: die
                # Rueckhalt-Bloecke (slot_dist/next_after/by_state) sind
                # boots-frei, ein "vertretbar" waere hier nicht belegbar.
                engine_boots = [r["item"] for r in result.get("items", [])
                                if r.get("kind") == "boots"]
                nxt = result.get("next")
                if nxt and nxt.get("kind") == "boots" and nxt["item"] not in engine_boots:
                    engine_boots.insert(0, nxt["item"])
                if not engine_boots:
                    # Die Engine sagt zu Boots nichts (typisch: der Spieler
                    # traegt im Vor-Kauf-Zustand schon T2-Boots und kauft ein
                    # zweites Paar). Ohne Aussage gibt es nichts zu messen ->
                    # gar nicht werten statt "miss gegen Leere".
                    continue
                # Konform ist der Kauf gegen die Engine-Vorschlaege ODER gegen
                # die ueblichen Boots der Kombi laut KB (s. BOOTS_OK_PICK_MIN).
                hit = (name in engine_boots
                       or kb_boots.get(name, 0.0) >= BOOTS_OK_PICK_MIN)
                btotal += 1
                bhits += int(hit)
                top = engine_boots[:3]
                grade = "hit" if hit else "miss"
            else:
                nxt = result.get("next")
                if not (nxt and nxt.get("item")):
                    # Engine hat fuer diesen Zustand nichts zu sagen (Build
                    # komplett / nur Elixier) -> nicht wertbar, ueberspringen.
                    continue
                # exclude_boots=True: Boots-Vorschlaege duerfen die Item-Top-3
                # nicht mitbelegen (sie werden im boots-Zweig separat gemessen).
                top = item_top3
                hit = name in top
                total += 1
                hits += int(hit)
                if hit:
                    grade = "hit"
                else:
                    # Troll-Guard: nicht die Neubewertung entscheidet, sondern
                    # eigene Evidenz fuer diesen Kauf. Rangfolge s. Modul-
                    # Docstring - der Konflikt-Ausschluss steht ueber allem.
                    has_boots = any(app_items.is_upgraded_boots(n)
                                    for n in owned_names)
                    # Kaufslot des laufenden Kaufs - Train-Definition wie
                    # recommend._current_slot (Boots zaehlen als Slot mit).
                    cur_slot = (app_items.count_completed(owned_ids)
                                + (1 if has_boots else 0) + 1)
                    if _blocked(name, sup, owned_names):
                        reason = None
                    elif name in kb_core:
                        reason = (f"Core-Item für {champ} {used_role or role} – "
                                  f"nur die Reihenfolge weicht ab")
                    elif name in top3_seen:
                        reason = (f"stand bei Min {top3_seen[name]} selbst in "
                                  f"der Engine-Top-3 – nur später gekauft")
                    else:
                        reason = _ok_reason(
                            name, sup, cur_slot=cur_slot, prev_items=prev_items,
                            gold_state=_gold_state(owned_ids, used_role, enemies),
                            owned_names=owned_names)
                    grade = "ok" if reason else "miss"
                    oks += int(bool(reason))
                prev_items.append(name)
            # Nach der Wertung merken, was die Engine hier empfohlen hat - ein
            # spaeterer Kauf aus dieser Liste ist dann kein blinder Griff mehr.
            for cand in item_top3:
                top3_seen[cand] = m
            purchases.append({"minute": m, "item": name, "kind": kind,
                              "hit": hit, "grade": grade, "ok_reason": reason,
                              "engine_top": top})

    return {"evaluable": True,
            "score": {"hits": hits, "ok": oks, "total": total},
            "boots": {"hits": bhits, "total": btotal},
            "purchases": purchases}
