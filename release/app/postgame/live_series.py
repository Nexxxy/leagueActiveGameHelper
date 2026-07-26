"""Serien-Builder aus dem Live-Dump (kein API-Key noetig).

Zweiter Datenquellen-Pfad neben `series.py` (Match-V5-Timeline): liest einen
Dump-Ordner `liveGameData/<spiel>/snapshot_*.json` (Zeitreihe ~5 s, je
`{dumped_at, raw, computed}`) und erzeugt **exakt dieselbe Ausgabe-Form** wie
`series.build_series` (`{frame_interval, n_frames, players, events}`), damit
`analysis.py`/`render.py` quellenunabhaengig weiterlaufen.

Der grosse Unterschied zur Timeline: der Dump hat KEINE participantIds, KEIN
Gegner-/Team-Gold und KEINEN Schaden pro Spieler. Deshalb hier:
  * **Synthetische, stabile pids** aus `allPlayers` (Team+Rolle-deterministisch),
  * **Item-/Power-Gold** (Σ `item.price` × `item.count`) als Gold-Metrik
    (identisch zur "ausgegebenes Gold"-Semantik des Timeline-Pfads),
  * **Vision** aus `scores.wardScore` (nicht Ward-Events),
  * **Schaden** nicht verfuegbar -> leere Serie (der Report setzt has_damage=False),
  * **Events** (`raw.events.Events[]`) ueber einen Namen->pid-Lookup abgebildet
    (Kills, Objectives, Tuerme), unbekannte Namen robust auf None gesetzt.

Reine Funktionen ohne Netz; IO nur das Einlesen der Snapshot-Dateien.
"""

import json
from pathlib import Path

# Live-Team ("ORDER"/"CHAOS") -> Riot-teamId (100/200), wie im Match-V5-Modell.
_TEAM_ID = {"ORDER": 100, "CHAOS": 200}

# Feste Rollen-Reihenfolge fuer die pid-Vergabe innerhalb eines Teams (die
# Live-`position` ist die Rolle). Unbekannte/leere Rollen wandern ans Ende.
_ROLE_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}

# Nur Summoner's Rift (CLASSIC) wird ausgewertet (s. plan_postgame.md §7).
_SR_MODES = frozenset({"CLASSIC"})


def _empty_series() -> dict:
    """Gleiche Keys wie series._empty_series + 'vision' (wardScore-Serie).

    'gold' und 'spent' sind im Dump identisch (beides Item-Gold), damit sowohl
    die Delta-Engine (nutzt 'gold') als auch die Graphen (nutzen 'spent')
    dieselbe key-freie Gold-Metrik sehen.
    'dmg'/'dmg_taken'/'cc_s'/'pos'/'cur_gold'/'xp' bleiben leer (im Dump nicht
    vorhanden); die drei Timeline-Serien dmg/dmg_taken/cc_s fuellt bei gueltigem
    Key die Anreicherung nach (s. enrich.fetch_damage_enrichment)."""
    return {"gold": [], "cur_gold": [], "spent": [], "cs": [], "xp": [],
            "level": [], "dmg": [], "dmg_taken": [], "cc_s": [], "pos": [],
            "vision": [], "items_ts": []}


# --- Snapshots laden --------------------------------------------------------

def load_snapshots(dump_dir) -> list[dict]:
    """Liest alle `snapshot_*.json` eines Dump-Ordners -> nach gameTime
    sortierte Liste der `raw`-Objekte (= Live `allgamedata`).

    Snapshots ohne `raw`/`allPlayers` werden uebersprungen. Wirft SystemExit,
    wenn der Ordner fehlt oder keinen brauchbaren Snapshot enthaelt."""
    d = Path(dump_dir)
    if not d.is_dir():
        raise SystemExit(f"[postgame] Dump-Ordner nicht gefunden: {d}")
    raws = []
    for path in sorted(d.glob("snapshot_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = (data or {}).get("raw") or {}
        if raw.get("allPlayers"):
            raws.append(raw)
    if not raws:
        raise SystemExit(f"[postgame] Keine verwertbaren Snapshots in {d}.")
    raws.sort(key=lambda r: (r.get("gameData", {}) or {}).get("gameTime", 0.0))
    return raws


def game_mode(snapshots: list[dict]) -> str:
    """gameMode des Dumps (aus dem ersten Snapshot)."""
    return (snapshots[0].get("gameData", {}) or {}).get("gameMode", "")


def is_supported(snapshots: list[dict]) -> bool:
    """True, wenn der Dump eine ausgewertete Queue (SR/CLASSIC) ist."""
    return game_mode(snapshots) in _SR_MODES


# --- pid-Synthese -----------------------------------------------------------

def build_pid_map(snapshots: list[dict]) -> dict:
    """Synthetische, stabile pids aus `allPlayers` des ersten Snapshots.

    Ordnung: Team ORDER (100er) vor CHAOS (200er), innerhalb des Teams nach
    Rollen-Reihenfolge (TOP,JUNGLE,MIDDLE,BOTTOM,UTILITY). Vergibt pro Team die
    pids <teamId>+1 .. <teamId>+5. Rueckgabe:
      {
        "parts":   [ {pid, team, role, champ, name, riotName}, ... ]  (10),
        "pid_team":{pid: teamId}, "pid_role":{pid: role},
        "name_to_pid": {riotIdGameName: pid},
      }
    Der Namen->pid-Lookup nutzt `riotIdGameName` (so heissen auch die Akteure im
    Event-Strom)."""
    players = snapshots[0].get("allPlayers", []) or []
    parts, pid_team, pid_role, name_to_pid = [], {}, {}, {}
    for team in ("ORDER", "CHAOS"):
        team_id = _TEAM_ID[team]
        members = [p for p in players if p.get("team") == team]
        members.sort(key=lambda p: _ROLE_ORDER.get(p.get("position", ""), 9))
        for i, p in enumerate(members, start=1):
            pid = team_id + i
            role = p.get("position", "") or ""
            riot = p.get("riotIdGameName", "") or ""
            tag = p.get("riotIdTagLine", "") or ""
            parts.append({
                "pid": pid, "team": team_id, "role": role,
                "champ": p.get("championName", "") or "",
                "name": f"{riot}#{tag}" if tag else (riot or f"P{pid}"),
                "riotName": riot,
            })
            pid_team[pid] = team_id
            pid_role[pid] = role
            if riot:
                name_to_pid[riot] = pid
    return {"parts": parts, "pid_team": pid_team, "pid_role": pid_role,
            "name_to_pid": name_to_pid}


# --- Minuten-Serien ---------------------------------------------------------

def _item_gold(player: dict) -> int:
    """Σ(price × count) ueber alle Items eines Spielers = Item-/Power-Gold."""
    total = 0
    for it in player.get("items", []) or []:
        total += (it.get("price", 0) or 0) * (it.get("count", 1) or 1)
    return total


def _snapshot_at(snapshots: list[dict], target_time: float) -> dict:
    """Letzter Snapshot mit gameTime <= target_time (Minuten-Bucketing).

    Gibt es keinen (Minute liegt vor dem ersten Snapshot), wird der erste
    Snapshot genommen, damit Frame 0 den Spielanfang zeigt."""
    chosen = snapshots[0]
    for r in snapshots:
        gt = (r.get("gameData", {}) or {}).get("gameTime", 0.0)
        if gt <= target_time:
            chosen = r
        else:
            break
    return chosen


def _players_by_name(raw: dict) -> dict:
    """{riotIdGameName: player-dict} eines Snapshots."""
    return {p.get("riotIdGameName", ""): p
            for p in (raw.get("allPlayers", []) or [])}


def extract_snapshot(raw: dict, pid_map: dict) -> dict:
    """Pro-Snapshot-Extraktion je synthetischer pid -> {pid: values|None}.

    Gemeinsamer Baustein fuer BEIDE Pfade (Datei-Dump ueber `_build_players` und
    das inkrementelle In-Memory-Capture, s. `capture.LiveCapture`): liest aus
    EINEM `raw`-Snapshot die Minuten-Werte (Item-Gold, cs, level, wardScore) je
    Spieler heraus. `None` fuer eine pid heisst 'Spieler in diesem Snapshot nicht
    gefunden' (defensiv - Roster ist eigentlich stabil); der Serien-Assembler
    (`assemble_players`) fuellt das dann mit dem letzten bekannten Wert auf.
    Nur pids mit `riotName` werden bedient (die uebrigen tragen leere Serien)."""
    by_name = _players_by_name(raw)
    out: dict[int, dict | None] = {}
    for p in pid_map["parts"]:
        riot = p["riotName"]
        if not riot:
            continue
        pl = by_name.get(riot)
        if pl is None:
            out[p["pid"]] = None
            continue
        scores = pl.get("scores", {}) or {}
        gold = _item_gold(pl)
        out[p["pid"]] = {
            "gold": gold, "spent": gold,   # im Dump identisch zu 'gold'
            "cs": scores.get("creepScore", 0) or 0,
            "level": pl.get("level", 0) or 0,
            "vision": scores.get("wardScore", 0.0) or 0.0,
            # Gehaltene Item-IDs dieser Minute (fuer die Build-Eval, §8b): der
            # Dump hat keine Kauf-Events, aber die aktuelle Item-Liste je Minute.
            "items": [it.get("itemID", 0) for it in (pl.get("items", []) or [])],
        }
    return out


def assemble_players(minute_extracts: list[dict], pid_map: dict) -> dict:
    """Minuten-Extrakte (je Frame ein `extract_snapshot`-Dict) -> Serien je pid.

    Gemeinsamer Serien-Assembler fuer Datei- und In-Memory-Pfad. `None` fuer eine
    pid in einem Frame wird mit dem letzten bekannten Wert (bzw. 0) aufgefuellt -
    identische Semantik wie der frueher inline in `_build_players` enthaltene
    Defensiv-Zweig, damit beide Pfade **byte-identische** Serien liefern."""
    players: dict[int, dict] = {p["pid"]: _empty_series() for p in pid_map["parts"]}
    for ext in minute_extracts:
        for pid, vals in ext.items():
            s = players[pid]
            if vals is None:
                # Roster stabil -> selten; defensiv letzter/0-Wert.
                for key in ("gold", "spent", "cs", "level", "vision"):
                    s[key].append(s[key][-1] if s[key] else 0)
                s["items_ts"].append(s["items_ts"][-1] if s["items_ts"] else [])
                continue
            s["gold"].append(vals["gold"])
            s["spent"].append(vals["spent"])
            s["cs"].append(vals["cs"])
            s["level"].append(vals["level"])
            s["vision"].append(vals["vision"])
            s["items_ts"].append(list(vals.get("items", [])))
    return players


def _build_players(snapshots: list[dict], pid_map: dict) -> dict:
    """Per-Minute-Serien je synthetischer pid (Frame-Index = Spielminute).

    Wert bei Minute M = Snapshot mit gameTime <= M·60 (letzter davor). Damit
    bleibt die Frame-Index=Minute-Konvention des Timeline-Builders erhalten. Die
    eigentliche Extraktion/Assemblierung teilen sich Datei- und In-Memory-Pfad
    (`extract_snapshot`/`assemble_players`)."""
    max_time = max((r.get("gameData", {}) or {}).get("gameTime", 0.0)
                   for r in snapshots)
    n_frames = int(max_time // 60) + 1
    minute_extracts = [extract_snapshot(_snapshot_at(snapshots, m * 60), pid_map)
                       for m in range(n_frames)]
    return assemble_players(minute_extracts, pid_map)


# --- Events -----------------------------------------------------------------

def _turret_team(name: str):
    """Riot-teamId des ZERSTOERTEN Turms aus seinem internen Namen.

    'Turret_TChaos_...' -> 200 (CHAOS-Turm), 'Turret_TOrder_...' -> 100. Damit
    passt das zur Timeline-Semantik (BUILDING_KILL.teamId = Team des Baus)."""
    low = name.lower()
    if "tchaos" in low:
        return 200
    if "torder" in low:
        return 100
    return None


def build_events_from_list(events_raw: list, pid_map: dict) -> dict:
    """Event-Stroeme aus einer bereits gewaehlten `Events[]`-Liste.

    Gemeinsamer Baustein fuer Datei- und In-Memory-Pfad. Der Live-Event-Strom ist
    kumulativ (jeder Poll enthaelt ALLE bisherigen Events mit EventID) - fuer den
    vollstaendigen Verlauf genuegt also die Liste EINES (des letzten) Polls.
    Namen (`riotIdGameName`) werden ueber den Lookup auf pids abgebildet;
    unbekannte Namen (Turret-/Minion-Execute, Fremdname) werden robust auf None
    gesetzt. Ausgabe-Form identisch zu series.build_series (kills/wards/elites/
    buildings/purchases); 'wards'/'purchases' bleiben leer (im Dump nicht
    enthalten - Vision laeuft ueber die wardScore-Serie)."""
    name_to_pid = pid_map["name_to_pid"]
    pid_team = pid_map["pid_team"]

    def pid_of(name):
        return name_to_pid.get(name)

    events = {"kills": [], "wards": [], "elites": [], "buildings": [],
              "purchases": []}
    for ev in events_raw:
        et = ev.get("EventName")
        ts = ev.get("EventTime", 0.0) or 0.0
        minute = ts / 60.0
        if et == "ChampionKill":
            events["kills"].append({
                "minute": minute, "ts": ts * 1000,
                "killer": pid_of(ev.get("KillerName")),
                "victim": pid_of(ev.get("VictimName")),
                "assists": [pid_of(a) for a in (ev.get("Assisters") or [])
                            if pid_of(a) is not None],
                "pos": None,
            })
        elif et in ("DragonKill", "BaronKill", "HordeKill"):
            killer = pid_of(ev.get("KillerName"))
            if et == "DragonKill":
                monster, subtype = "DRAGON", ev.get("DragonType") or ""
            elif et == "BaronKill":
                monster, subtype = "BARON_NASHOR", ""
            else:
                monster, subtype = "HORDE", "Grubs"
            events["elites"].append({
                "minute": minute,
                "killer": killer,
                "team": pid_team.get(killer),
                "monster": monster, "subtype": subtype,
                "assists": [pid_of(a) for a in (ev.get("Assisters") or [])
                            if pid_of(a) is not None],
                "pos": None,
            })
        elif et == "TurretKilled":
            events["buildings"].append({
                "minute": minute,
                "killer": pid_of(ev.get("KillerName")),
                "team": _turret_team(ev.get("TurretKilled", "") or ""),
                "building": "TOWER_BUILDING",
                "tower": None, "lane": None,
                "assists": [pid_of(a) for a in (ev.get("Assisters") or [])
                            if pid_of(a) is not None],
            })
    return events


def latest_events(snapshots: list[dict]) -> list:
    """Letzte nicht-leere `Events[]`-Liste ueber alle Snapshots (kumulativ ->
    vollstaendiger Verlauf). Leere Liste, wenn kein Snapshot Events traegt."""
    for raw in reversed(snapshots):
        evs = ((raw.get("events", {}) or {}).get("Events", []) or [])
        if evs:
            return evs
    return []


def _build_events(snapshots: list[dict], pid_map: dict) -> dict:
    """Event-Stroeme aus dem letzten Snapshot mit Events (Datei-Pfad)."""
    return build_events_from_list(latest_events(snapshots), pid_map)


# --- Oeffentlicher Einstieg -------------------------------------------------

def build_series_from_dump(snapshots: list[dict], pid_map: dict) -> dict:
    """Dump-Snapshots -> {frame_interval, n_frames, players, events} (gleiche
    Form wie series.build_series). `frame_interval` ist 60000 (Minuten-Slots),
    damit Frame-Index == Minute gilt (Konvention des Timeline-Builders)."""
    players = _build_players(snapshots, pid_map)
    events = _build_events(snapshots, pid_map)
    n_frames = len(next(iter(players.values()))["gold"]) if players else 0
    return {"frame_interval": 60000, "n_frames": n_frames,
            "players": players, "events": events}


def item_name_lookup_from_dump(snapshots: list[dict]):
    """Callable itemId(int) -> Item-Name(str)|None aus den Dump-Items selbst.

    Der Dump traegt `displayName` je Item -> die ID->Name-Abbildung ist key- und
    netz-frei direkt aus den Snapshots baubar (kein Data-Dragon noetig)."""
    names: dict[int, str] = {}
    for raw in snapshots:
        collect_item_names(raw, names)
    return name_lookup(names)


def collect_item_names(raw: dict, names: dict) -> dict:
    """Sammelt itemID->displayName aus EINEM Snapshot in `names` (in place).

    Gemeinsamer Baustein: der Datei-Pfad laeuft ueber alle Snapshots, das
    In-Memory-Capture ruft das je Poll auf und haelt so den Lookup aktuell
    (Item-Namen sind netz-/keyfrei direkt in den Live-Daten)."""
    for pl in raw.get("allPlayers", []) or []:
        for it in pl.get("items", []) or []:
            iid = it.get("itemID")
            nm = it.get("displayName")
            if iid and nm:
                names[int(iid)] = nm
    return names


def name_lookup(names: dict):
    """Callable itemId(int) -> Item-Name(str)|None ueber ein id->name-Dict."""
    def lookup(iid):
        return names.get(int(iid)) if iid else None
    return lookup


def item_gold_lookup_from_dump(snapshots: list[dict]):
    """Callable itemId(int) -> Gesamt-Gold(int) aus den Dump-Items (Preis).

    Symmetrisch zu `item_name_lookup_from_dump`: der Dump traegt `price` je Item
    -> die ID->Gold-Abbildung ist key-/netzfrei direkt aus den Snapshots baubar
    (fuer die Build-Eval-'fertig'-Schwelle, §8b)."""
    prices: dict[int, int] = {}
    for raw in snapshots:
        for pl in raw.get("allPlayers", []) or []:
            for it in pl.get("items", []) or []:
                iid = it.get("itemID")
                price = it.get("price")
                if iid and price:
                    prices[int(iid)] = int(price)
    def lookup(iid):
        return prices.get(int(iid), 0) if iid else 0
    return lookup


def final_stats_from_raw(raw: dict, pid_map: dict) -> dict:
    """Endwerte je pid aus EINEM Snapshot (fuer Lobby-Ranking + KDA + Items).

    Gemeinsamer Baustein: der Datei-Pfad reicht den letzten Snapshot herein, das
    In-Memory-Capture ueberschreibt diesen Wert bei jedem Poll (der jeweils
    letzte Poll = Endstand). Rueckgabe {pid: {gold, cs, dmg, vision, kills,
    deaths, assists, items}}; `dmg`=0 (im Dump nicht verfuegbar)."""
    by_name = _players_by_name(raw)
    out: dict[int, dict] = {}
    for p in pid_map["parts"]:
        pl = by_name.get(p["riotName"], {}) or {}
        scores = pl.get("scores", {}) or {}
        items = [it.get("itemID", 0) for it in (pl.get("items", []) or [])]
        out[p["pid"]] = {
            "gold": _item_gold(pl),
            "cs": scores.get("creepScore", 0) or 0,
            "dmg": 0,
            "vision": scores.get("wardScore", 0.0) or 0.0,
            "kills": scores.get("kills", 0) or 0,
            "deaths": scores.get("deaths", 0) or 0,
            "assists": scores.get("assists", 0) or 0,
            "items": items,
        }
    return out


def final_stats(snapshots: list[dict], pid_map: dict) -> dict:
    """Endwerte je pid aus dem letzten Snapshot (Datei-Pfad)."""
    return final_stats_from_raw(snapshots[-1], pid_map)


def resolve_me_name(snapshots: list[dict]) -> str | None:
    """Riot-ID 'Name#Tag' des activePlayer (= der Nutzer, der gedumpt hat).

    Der activePlayer traegt `riotIdGameName`/`riotIdTagLine`; daraus die
    Identitaet ableiten, falls in der config kein `me:` gesetzt ist."""
    return resolve_me_name_from_raw(snapshots[0])


def resolve_me_name_from_raw(raw: dict) -> str | None:
    """Riot-ID 'Name#Tag' des activePlayer aus EINEM Snapshot (oder None).

    Im Live-Betrieb IST der activePlayer der Nutzer - deshalb die primaere
    Identitaetsquelle des In-Memory-Reports (config `me:` nur Fallback)."""
    ap = (raw or {}).get("activePlayer", {}) or {}
    name = ap.get("riotIdGameName")
    if not name:
        return None
    tag = ap.get("riotIdTagLine") or ""
    return f"{name}#{tag}" if tag else name
