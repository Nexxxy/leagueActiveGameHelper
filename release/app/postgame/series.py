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
    dokumentierte Toleranz akzeptiert (Median 0, ~2.8 % Lobby-Summe)."""
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
            elif et in ("ITEM_SOLD", "ITEM_DESTROYED"):
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

    return {"frame_interval": interval, "n_frames": len(frames),
            "players": players, "events": events}


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
