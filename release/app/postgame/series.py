"""Serien-Builder: Match-V5-Timeline -> Minuten-Serien je Spieler + Event-Stroeme.

Reine Funktion ohne IO/Netz (offline testbar). Aus den `participantFrames` jeder
Minute werden pro Spieler kumulative Serien gezogen (totalGold, CS, XP, Level,
Schaden-an-Champions, erlittener Schaden, Position); aus `frames[].events[]` die
fuer den Report relevanten Event-Stroeme (Kills, Wards, Elite-Monster, Gebaeude,
Item-Kaeufe). Der Frame-Index entspricht bei `frameInterval == 60000` genau der
Spielminute.

**Gold-Metrik (Entscheidung 2026-07-24, s. plan_postgame.md §2.2b):** `spent` ist
das **gehaltene Item-Gold** (Σ `gold.total` der zu dieser Minute im Inventar
liegenden Items) = Kampfkraft auf der Karte - dieselbe Semantik wie der Dump-Pfad
(`live_series`, Σ `item.price`). Das ersetzt die fruehere `totalGold - currentGold`
-Rechnung (verdient - gebankt), die als `earned_minus_bank` erhalten bleibt (wird
aber nirgends mehr geplottet). Voraussetzung fuer `spent` ist ein `item_gold`-
Lookup (itemId -> gold.total); ohne ihn faellt `spent` auf `earned_minus_bank`
zurueck (z. B. im Enrich-Pfad, der `spent` gar nicht nutzt).
"""


# Echte Ward-Typen fuer den Vision-Proxy. Riot emittiert in Match-V5-Timelines
# massenhaft WARD_PLACED-Events mit wardType "UNDEFINED" fuer Nicht-Standard-
# Vision-Entitaeten (Runen-/Effekt-Wards) - bei einem einzelnen Spieler wurden so
# 365 Phantom-Events beobachtet (real 9 Wards laut Match-Summary). Nur diese vier
# echten Ward-Typen zaehlen in die Vision-Aggregationen. Ein FEHLENDER Typ (None)
# wird toleriert, weil der Live-Event-Strom (Dump/Capture) gar keinen wardType
# traegt und dort kein UNDEFINED-Spam auftritt - so bleibt der Filter tolerant.
REAL_WARD_TYPES = {"YELLOW_TRINKET", "BLUE_TRINKET", "SIGHT_WARD", "CONTROL_WARD"}


def is_real_ward(ward_type) -> bool:
    """True, wenn ein Ward-Event zum Vision-Proxy zaehlt: echter Ward-Typ ODER
    fehlender Typ (None). Filtert damit gezielt das UNDEFINED-Rauschen der
    Timeline heraus, ohne die Live-Events (ohne wardType) zu verlieren."""
    return ward_type is None or ward_type in REAL_WARD_TYPES


def _empty_series() -> dict:
    return {"gold": [], "cur_gold": [], "spent": [], "earned_minus_bank": [],
            "cs": [], "xp": [], "level": [], "dmg": [], "dmg_taken": [],
            "cc_s": [], "pos": [], "items_ts": []}


def _remove_one(lst: list, item) -> None:
    """Entfernt die erste Instanz von `item` aus `lst` (in place), falls vorhanden.
    Analog zu backtest._remove_one - ein Verkauf/Destroy/Undo trifft genau eine
    Instanz, nicht alle Stacks."""
    if item in lst:
        lst.remove(item)


# --- Besessenheits-Korruption (Viego) ---------------------------------------
# WARUM: Riot emittiert in Match-V5-Timelines bei Viegos Passive (er uebernimmt
# den getoeteten Champion samt dessen Inventar) massenhaft ITEM_DESTROYED-Events
# auf VIEGOS participantId - sowohl fuer seine EIGENEN gehaltenen Items als auch
# fuer die des besessenen Champions, die er nie gekauft hat. Wiederhergestellt
# wird nichts: es gibt keine Gegen-Events. Real gemessen (EUW1_7933910870, Viego
# = pid 7): 156 Item-Events, davon Dutzende Phantom-Destroys; das naive Replay
# loeschte damit reihenweise seine echten Items, die `spent`-Serie fiel wiederholt
# auf 0 und `items_ts` endete mit [3031, 1029] statt des echten 6-Item-Builds.
#
# ERKENNUNG (zwei Kriterien, beide muessen greifen - empirisch kalibriert an 150
# gecachten 16.15-Timelines = 1500 Spieler, davon 24 Viego):
#
#  1. **Burst-Groesse** (das eigentliche Trennmerkmal): ein Event-Burst
#     (identischer `timestamp`) OHNE Kauf desselben Spielers, der >= 4
#     ITEM_DESTROYED enthaelt. Gemessen: KEIN einziger der 1476 Nicht-Viego-
#     Spieler hatte je einen No-Kauf-Burst mit mehr als 3 Destroys (legitim sind
#     nur kleine Bursts: Consumables, Pet-Evolution, Boots-Upgrade); ALLE 24
#     Viegos lagen bei 5-7. Die Schwelle 4 liegt exakt in der leeren Luecke.
#  2. **Phantom-Destroys**: >= 3 Destroys auf Items, die der Spieler zu dem
#     Zeitpunkt gar nicht haelt. Allein ist dieses Kriterium NICHT brauchbar
#     (gemessen: 307 von 400 Spielern reissen 3 - Runen-Biskuits, Support-Wards,
#     Trinkets und Pets werden ohne Kauf zerstoert); es dient nur als zweite
#     Plausibilitaets-Klammer, damit ein grosser Burst allein noch kein
#     Reparatur-Replay ausloest.
CORRUPT_BURST_DESTROYS = 4
PHANTOM_DESTROY_LIMIT = 3


def _phantom_destroy_counts(frames: list, pids=None) -> dict:
    """Je participantId: Zahl der ITEM_DESTROYED-Events auf einem Item, das der
    Spieler zu diesem Zeitpunkt gar nicht haelt (nie gekauft bzw. schon wieder
    weg). Erwerb = ITEM_PURCHASED oder ITEM_UNDO-`afterId` (wiederhergestellter
    Verkauf); Abgang = SOLD/DESTROYED/UNDO-`beforeId`.

    `pids`: optionale Einschraenkung auf bekannte Spieler (sonst alle).
    Diagnose-Funktion ohne Seiteneffekt - Basis fuer `_corrupt_pids`."""
    held: dict = {}
    out: dict = {}
    for frame in frames:
        for ev in frame.get("events", []) or []:
            pid = ev.get("participantId")
            if pid is None or (pids is not None and pid not in pids):
                continue
            et = ev.get("type")
            if et == "ITEM_PURCHASED":
                held.setdefault(pid, []).append(ev.get("itemId"))
            elif et == "ITEM_UNDO":
                before, after = ev.get("beforeId"), ev.get("afterId")
                if before:
                    _remove_one(held.setdefault(pid, []), before)
                if after:
                    held.setdefault(pid, []).append(after)
            elif et == "ITEM_SOLD":
                _remove_one(held.setdefault(pid, []), ev.get("itemId"))
            elif et == "ITEM_DESTROYED":
                bag = held.setdefault(pid, [])
                out.setdefault(pid, 0)
                iid = ev.get("itemId")
                if iid in bag:
                    bag.remove(iid)
                else:
                    out[pid] += 1
    return out


def _item_bursts(frames: list, pids=None) -> dict:
    """Item-Events je (pid, timestamp) buendeln -> {(pid, ts): (kaeufe, destroys)}.

    Riot legt die Destroys der beim Zusammenbau verbrauchten Komponenten auf
    EXAKT denselben `timestamp` wie den Kauf des fertigen Items (verifiziert an
    EUW1_7933910870: ts=916128 zerstoert 6690/3051/1043 und kauft 6672). Ein
    Burst mit Kauf ist damit ein echter Zusammenbau; die Besessenheits-Phantoms
    stehen immer in reinen Destroy-Bursts."""
    out: dict = {}
    for frame in frames:
        for ev in frame.get("events", []) or []:
            pid = ev.get("participantId")
            if pid is None or (pids is not None and pid not in pids):
                continue
            et = ev.get("type")
            if et not in ("ITEM_PURCHASED", "ITEM_DESTROYED"):
                continue
            key = (pid, ev.get("timestamp"))
            buys, kills = out.get(key, (0, 0))
            if et == "ITEM_PURCHASED":
                out[key] = (buys + 1, kills)
            else:
                out[key] = (buys, kills + 1)
    return out


def _corrupt_pids(frames: list, pids=None) -> tuple[set, set]:
    """(korrumpierte pids, (pid, ts)-Paare mit Kauf) - s. Modul-Kommentar oben.

    Korrumpiert ist ein Spieler, wenn er sowohl einen No-Kauf-Burst mit >=
    CORRUPT_BURST_DESTROYS Destroys hat ALS AUCH >= PHANTOM_DESTROY_LIMIT
    Phantom-Destroys. Fuer alle anderen bleibt das Replay unveraendert
    (Paritaets-Garantie fuer saubere Timelines).

    Die Kauf-Stempel fallen im selben Durchgang an und werden mitgegeben, damit
    das Reparatur-Replay die Timeline nicht ein drittes Mal durchlaufen muss."""
    bursts = _item_bursts(frames, pids)
    buy_stamps = {key for key, (buys, _k) in bursts.items() if buys}
    big = {pid for (pid, _ts), (buys, kills) in bursts.items()
           if not buys and kills >= CORRUPT_BURST_DESTROYS}
    if not big:
        return set(), buy_stamps
    phantoms = _phantom_destroy_counts(frames, big)
    return ({pid for pid in big
             if phantoms.get(pid, 0) >= PHANTOM_DESTROY_LIMIT}, buy_stamps)


def _inventory_ids(frames: list, players: dict) -> dict:
    """Inventar-Replay je Spieler -> `items_ts`-Serie (Liste der zum Ende jeder
    Minute gehaltenen Item-IDs).

    Spielt die Item-Events (ITEM_PURCHASED/SOLD/UNDO/DESTROYED) chronologisch
    durch und schreibt am Ende jedes Frames eine KOPIE des aktuellen Inventars
    (Item-IDs) je pid. Aus diesen ID-Snapshots leitet `build_series` sowohl die
    `spent`-Serie (Σ gold.total, sofern ein item_gold-Lookup vorliegt) als auch
    den je-Minute gehaltenen Item-Stand fuer die Build-Eval Stufe 3 (Engine-Replay,
    §8b) ab - key-frei und in exakt derselben Form wie der Dump-Pfad
    (`live_series.items_ts`).

    **Empirisch verifiziert (16.14-Timelines, 1000+ Spieler):** Riot feuert fuer
    beim Item-Zusammenbau verbrauchte Komponenten ITEM_DESTROYED - das gehaltene
    Inventar entspricht damit direkt der Item-Menge auf der Karte, ohne dass der
    Rezeptbaum aufgeloest werden muss. Die verbleibenden Abweichungen zum
    Match-Endwert stammen NICHT von haengenden Komponenten, sondern von
    ereignislosen Aufwertungen: Auto-Transformationen (Manamune->Muramana,
    Winter's Approach->Fimbulwinter, Archangel's->Seraph's, Tear-Linie),
    Season-2026-Boots-Upgrades (in-place) und verbrauchten Consumables/Trinkets
    (0-500 G) - allesamt klein und ohne eigenes Timeline-Event, daher als
    dokumentierte Toleranz akzeptiert (Median 0, ~2.8 % Lobby-Summe).

    **Besessenheits-Haertung (Bugfix 2026-07-30):** Spieler, deren Event-Spur
    durch Viegos Passive korrumpiert ist (s. `_corrupt_pids`), laufen ueber ein
    Reparatur-Replay: fuer sie zaehlt ein ITEM_DESTROYED nur, wenn im selben
    Event-Burst (identischer `timestamp`) auch ein ITEM_PURCHASED desselben
    Spielers liegt - also nur der echte Komponenten-Verbrauch beim Zusammenbau.
    Alle uebrigen Destroys sind Phantoms und werden ignoriert. Rest-Toleranz:
    verbrauchte Consumables und Pet-Eier des korrumpierten Spielers bleiben im
    Replay haengen (wenige hundert Gold) - bewusst akzeptiert, weil nur der
    ohnehin korrupte Spieler betroffen ist und die Alternative (Inventar faellt
    auf 0) massiv schlechter war. Fuer ALLE anderen Spieler ist das Replay
    unveraendert (Paritaets-Garantie)."""
    corrupt, buy_stamps = _corrupt_pids(frames, set(players))
    inv: dict[int, list] = {pid: [] for pid in players}
    snapshots: dict[int, list] = {pid: [] for pid in players}
    for frame in frames:
        for ev in frame.get("events", []) or []:
            et = ev.get("type")
            pid = ev.get("participantId")
            if pid not in inv:
                continue
            if et == "ITEM_PURCHASED":
                inv[pid].append(ev.get("itemId"))
            elif et == "ITEM_DESTROYED":
                # Korrumpierter Spieler: nur Destroys im Kauf-Burst sind echt.
                if pid in corrupt and (pid, ev.get("timestamp")) not in buy_stamps:
                    continue
                _remove_one(inv[pid], ev.get("itemId"))
            elif et == "ITEM_SOLD":
                _remove_one(inv[pid], ev.get("itemId"))
            elif et == "ITEM_UNDO":
                # Undo macht einen Kauf ODER einen Verkauf rueckgaengig:
                # beforeId = gekauftes Item zurueckgeben, afterId = verkauftes
                # Item wiederherstellen (Felder wie in backtest/matchindex).
                before, after = ev.get("beforeId"), ev.get("afterId")
                if before:
                    _remove_one(inv[pid], before)
                if after:
                    inv[pid].append(after)
        for pid, items in inv.items():
            snapshots[pid].append(list(items))   # Kopie: Stand zum Frame-Ende
    return snapshots


def build_series(timeline: dict, item_gold=None) -> dict:
    """Timeline-Dict -> {frame_interval, n_frames, players, events}.

    `item_gold`: Callable itemId(int) -> gold.total(int). Ist es gesetzt, ist
    `spent` das gehaltene Item-Gold (Inventar-Replay); ohne Lookup faellt `spent`
    auf `earned_minus_bank` (verdient - gebankt) zurueck.

    players: {pid(int): {gold, cur_gold, spent, earned_minus_bank, cs, xp, level,
    dmg, dmg_taken, cc_s, pos}}, jeweils eine Liste ueber die Frames (Index =
    Minute bei 60s-Frames); `cc_s` = kumulative CC-Zeit in SEKUNDEN (Riot liefert
    `timeEnemySpentControlled` in Millisekunden).
    events: {kills, wards, elites, buildings, purchases} - flache
    Listen mit Minute + rollen-/spieler-bezogenen Feldern (participantId-basiert)."""
    info = timeline.get("info", {}) or {}
    frames = info.get("frames", []) or []
    interval = info.get("frameInterval", 60000) or 60000

    players: dict[int, dict] = {}
    for frame in frames:
        pfs = frame.get("participantFrames", {}) or {}
        for key, pf in pfs.items():
            pid = int(pf.get("participantId", key))
            s = players.setdefault(pid, _empty_series())
            total_gold = pf.get("totalGold", 0) or 0
            cur_gold = pf.get("currentGold", 0) or 0
            s["gold"].append(total_gold)
            s["cur_gold"].append(cur_gold)
            # Alte Gold-Metrik (verdient - gebankt): bleibt als Referenz erhalten,
            # wird aber nicht mehr geplottet (spent = gehaltenes Item-Gold, s.u.).
            s["earned_minus_bank"].append(max(0, total_gold - cur_gold))
            s["cs"].append((pf.get("minionsKilled", 0) or 0)
                           + (pf.get("jungleMinionsKilled", 0) or 0))
            s["xp"].append(pf.get("xp", 0) or 0)
            s["level"].append(pf.get("level", 0) or 0)
            dmg = pf.get("damageStats", {}) or {}
            s["dmg"].append(dmg.get("totalDamageDoneToChampions", 0) or 0)
            # ROHER erlittener Schaden (nicht damageSelfMitigated) - im Report als
            # "Erlitten" gelabelt, dokumentierter Proxy fuer Frontline-Praesenz.
            s["dmg_taken"].append(dmg.get("totalDamageTaken", 0) or 0)
            # CC-Zeit: `timeEnemySpentControlled` ist KUMULATIV und liegt in
            # MILLISEKUNDEN vor (verifiziert an echten Timelines: ~15.063 @Min10
            # -> 56.566 @Min20). Wir speichern Sekunden (1 Nachkommastelle), damit
            # die Serie ohne weitere Umrechnung menschenlesbar bleibt.
            s["cc_s"].append(
                round((pf.get("timeEnemySpentControlled", 0) or 0) / 1000.0, 1))
            pos = pf.get("position", {}) or {}
            s["pos"].append((pos.get("x"), pos.get("y")))

    # Inventar-Replay (Item-IDs je Frame-Ende) - Basis fuer items_ts UND spent.
    # items_ts (je-Minute gehaltene IDs) liegt damit key-frei in derselben Form
    # vor wie im Dump-Pfad (live_series) und speist die Build-Eval Stufe 3.
    inv_ids = _inventory_ids(frames, players)
    for pid, s in players.items():
        s["items_ts"] = inv_ids[pid]
    # spent = gehaltenes Item-Gold (Σ gold.total der gehaltenen IDs), sonst
    # Fallback auf die alte earned_minus_bank-Serie (gleiche Laenge, nie geplottet).
    if item_gold is not None:
        for pid, s in players.items():
            s["spent"] = [sum(item_gold(i) for i in ids) for ids in inv_ids[pid]]
    else:
        for s in players.values():
            s["spent"] = list(s["earned_minus_bank"])

    events = {"kills": [], "wards": [], "elites": [], "buildings": [],
              "purchases": []}
    for t, frame in enumerate(frames):
        for ev in frame.get("events", []) or []:
            et = ev.get("type")
            ts = ev.get("timestamp", t * interval)
            minute = ts / 60000.0
            if et == "CHAMPION_KILL":
                events["kills"].append({
                    "minute": minute, "ts": ts,
                    "killer": ev.get("killerId"),
                    "victim": ev.get("victimId"),
                    "assists": list(ev.get("assistingParticipantIds", []) or []),
                    "pos": ev.get("position"),
                })
            elif et in ("WARD_PLACED", "WARD_KILL"):
                events["wards"].append({
                    "minute": minute, "kind": et,
                    "creator": ev.get("creatorId"),   # WARD_PLACED
                    "killer": ev.get("killerId"),      # WARD_KILL
                    "ward_type": ev.get("wardType"),
                })
            elif et == "ELITE_MONSTER_KILL":
                events["elites"].append({
                    "minute": minute,
                    "killer": ev.get("killerId"),
                    "team": ev.get("killerTeamId"),
                    "monster": ev.get("monsterType"),
                    "subtype": ev.get("monsterSubType"),
                    "assists": list(ev.get("assistingParticipantIds", []) or []),
                    "pos": ev.get("position"),
                })
            elif et == "BUILDING_KILL":
                events["buildings"].append({
                    "minute": minute,
                    "killer": ev.get("killerId"),
                    "team": ev.get("teamId"),          # Team des zerstoerten Baus
                    "building": ev.get("buildingType"),
                    "tower": ev.get("towerType"),
                    "lane": ev.get("laneType"),
                    "assists": list(ev.get("assistingParticipantIds", []) or []),
                })
            elif et == "ITEM_PURCHASED":
                events["purchases"].append({
                    "minute": minute,
                    "pid": ev.get("participantId"),
                    "item": ev.get("itemId"),
                })

    # data_start = 0: die Match-V5-Timeline beginnt IMMER bei Spielbeginn, also
    # ist jeder Frame gemessen. Das Feld existiert nur, damit Timeline- und
    # Live-Pfad dieselbe Serien-Form haben (der Live-Pfad kann spaeter starten,
    # s. live_series.data_start_minute).
    return {"frame_interval": interval, "n_frames": len(frames),
            "data_start": 0, "players": players, "events": events}


def team_series(series: dict, pid_team: dict, metric: str) -> dict:
    """Summiert eine Spieler-Serie (z. B. 'gold') je Frame pro Team.

    `pid_team`: {pid -> teamId (100/200)}. Rueckgabe {teamId: [summe_je_frame]}.
    Kuerzeste Spieler-Serie bestimmt die Laenge (Frames, die nicht fuer alle
    vorliegen, werden ausgelassen)."""
    players = series["players"]
    out: dict[int, list] = {}
    for team in (100, 200):
        pids = [p for p, t in pid_team.items() if t == team and p in players]
        if not pids:
            out[team] = []
            continue
        n = min(len(players[p][metric]) for p in pids)
        out[team] = [sum(players[p][metric][i] for p in pids) for i in range(n)]
    return out


def team_kill_series(series: dict, pid_team: dict) -> dict:
    """Kumulierte Team-Kills je Team ueber die Frames (key-frei in allen Pfaden).

    Zaehlt CHAMPION_KILL-Events dem Team des Killers zu (pid_team) und kumuliert
    bis zur jeweiligen Minute. Rueckgabe {teamId: [kumwert_je_frame]}. Der
    Event-Strom `events.kills` liegt sowohl im Timeline- als auch im Live-Dump-
    Pfad vor - deshalb ist der Kill-Graph key-frei."""
    n = series["n_frames"]
    out = {100: [0] * n, 200: [0] * n}
    for k in series["events"]["kills"]:
        team = pid_team.get(k["killer"])
        if team not in out:
            continue
        idx = int(k["minute"])
        if idx >= n:
            idx = n - 1
        if idx < 0:
            continue
        out[team][idx] += 1
    for team in (100, 200):
        run = 0
        for i in range(n):
            run += out[team][i]
            out[team][i] = run
    return out


def team_vision_series(series: dict, pid_team: dict) -> dict:
    """Kumulierte Vision-Proxy-Serie je Team ueber die Frames: gelegte + zerstoerte
    Wards des Teams, aufsummiert bis zur jeweiligen Minute. Rueckgabe
    {teamId: [kumwert_je_frame]}. Timeline liefert keinen Visionscore ueber Zeit -
    Ward-Aktionen sind der beste zeitaufgeloeste Proxy. Es zaehlen nur echte
    Ward-Typen (is_real_ward) - das UNDEFINED-Rauschen der Timeline bleibt aussen
    vor, sonst verzerren Runen-/Effekt-Wards die Y-Skala massiv."""
    n = series["n_frames"]
    out = {100: [0] * n, 200: [0] * n}
    for w in series["events"]["wards"]:
        if not is_real_ward(w.get("ward_type")):
            continue
        pid = w["creator"] if w["kind"] == "WARD_PLACED" else w["killer"]
        team = pid_team.get(pid)
        if team not in out:
            continue
        idx = int(w["minute"])
        if idx >= n:
            idx = n - 1
        if idx < 0:
            continue
        out[team][idx] += 1
    # kumulieren
    for team in (100, 200):
        run = 0
        for i in range(n):
            run += out[team][i]
            out[team][i] = run
    return out
