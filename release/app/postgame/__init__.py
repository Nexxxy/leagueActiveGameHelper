"""Post-Game-Report (Phase 1): aus EINER Match-ID einen HTML-Report bauen.

Massstab ist die tatsaechlich gespielte Lobby, nicht High-Elo (s.
docu/archive/plans/plan_postgame.md). Der Ablauf: Match + Timeline laden (Cache-first) ->
Serien-Builder (Timeline -> Minuten-Serien + Events) -> Gegenpart-Delta-Engine
je Team-Spieler -> Lobby-Ranking + Item-Sanity + Narrativ -> self-contained
HTML nach `postgame/<matchId>.html`.

Oeffentliche Einstiegspunkte:
  build_report(cfg, match_id, ...) -> report-Dict (reines Modell, testbar)
  run(cfg, match_id, ...)          -> schreibt die HTML-Datei, gibt den Pfad
"""

from pathlib import Path

from core import yamlio
from core.config import Config
# `capture` wird hier nicht direkt benutzt, aber bewusst eager mitgeladen: der
# Release-Smoketest importiert `app.postgame.capture` und wuerde ein fehlendes
# Submodul im Paket sonst erst zur Laufzeit bemerken.
from . import (analysis, build_replay, capture, enrich, fetch,  # noqa: F401
               live_series, render, series, trend)

# Anzeige-Namen der relevanten Queues (SR 5v5).
_QUEUE_NAME = {420: "Ranked Solo/Duo", 440: "Ranked Flex",
               400: "Normal Draft", 430: "Normal Blind",
               490: "Quickplay", 480: "Swiftplay"}


def _final_stats(p: dict) -> dict:
    """Endwerte eines Participants aus den Match-Stats (fuer Lobby-Ranking)."""
    return {
        "gold": p.get("goldEarned", 0) or 0,
        "cs": (p.get("totalMinionsKilled", 0) or 0)
              + (p.get("neutralMinionsKilled", 0) or 0),
        "dmg": p.get("totalDamageDealtToChampions", 0) or 0,
        "vision": p.get("visionScore", 0) or 0,
    }


def _load_core_sets(cfg: Config, patch: str) -> dict:
    """{champ: {role: [core-item-namen]}} aus builds.yaml des Patches.

    Die Core-Items werden nach `avg_slot` sortiert (=KB-Kaufreihenfolge, fuer die
    Build-Eval-Reihenfolge §8b); fehlt avg_slot (z. B. Mini-Fixture), gilt die
    yaml-Reihenfolge. Fehlt die Datei, leeres Dict (Item-Sanity + Build-Eval
    entfallen dann sauber)."""
    path = cfg.out_dir / patch / "builds.yaml"
    if not path.exists():
        return {}
    data = yamlio.load(path) or {}
    champs = data.get("champions", {}) or {}
    out: dict = {}
    for champ, roles in champs.items():
        out[champ] = {}
        for role, entry in (roles or {}).items():
            items = [(c.get("item"), c.get("avg_slot"))
                     for c in (entry or {}).get("core", []) if c.get("item")]
            # Stabil nach avg_slot sortieren; None (kein Slot) ans Ende.
            items.sort(key=lambda t: (t[1] is None, t[1] if t[1] is not None else 0))
            out[champ][role] = [name for name, _slot in items]
    return out


def build_report(cfg: Config, match_id: str, *, me: str | None = None,
                 retries: int = 0, backoff: float = 15.0, log=print) -> dict:
    """Baut das Report-Modell (Dict) fuer eine Match-ID. Reine Datenaufbereitung
    ohne HTML - so bleibt der Renderer entkoppelt und die Logik testbar."""
    patch, match, timeline = fetch.load_match_and_timeline(
        cfg, match_id, retries=retries, backoff=backoff, log=log)
    info = match["info"]
    parts = info["participants"]

    me_pid = fetch.resolve_me_pid(cfg, match, me, log=log)
    if me_pid is None:
        # Ohne Identitaet: Team 100 als "eigenes" Team nehmen, damit der Report
        # dennoch entsteht (Phase 1, expliziter Aufruf mit fremder ID).
        me_pid = parts[0].get("participantId")
        log("[postgame] Kein 'me' aufloesbar - Team 100 als eigenes Team gewaehlt.")

    # pid -> participant + Kompakt-Metadaten.
    by_pid = {p["participantId"]: p for p in parts}
    me_part = by_pid[me_pid]
    my_team = me_part["teamId"]

    meta_parts = [{"pid": p["participantId"], "team": p["teamId"],
                   "role": p.get("teamPosition") or ""} for p in parts]
    cmap = analysis.counterpart_map(meta_parts)
    pid_team = {p["participantId"]: p["teamId"] for p in parts}

    # Item-Lookups (Name + gold.total). gold.total speist die einheitliche
    # Gold-Metrik: spent = gehaltenes Item-Gold (Inventar-Replay, s. series).
    id_to_name = fetch.item_name_lookup(cfg, patch)
    id_to_gold = fetch.item_gold_lookup(cfg, patch)
    ser = series.build_series(timeline, item_gold=id_to_gold)

    # Team-Kills je Team (fuer Kill-Participation).
    team_kills = {100: 0, 200: 0}
    for p in parts:
        team_kills[p["teamId"]] += p.get("kills", 0) or 0

    # Lobby-Ranking ueber alle 10 (Endwerte).
    rank_input = [{"pid": p["participantId"], **_final_stats(p)} for p in parts]
    ranking = analysis.lobby_ranking(rank_input)

    core_sets = _load_core_sets(cfg, patch)

    def _name(p):
        n = p.get("riotIdGameName") or p.get("summonerName") or ""
        t = p.get("riotIdTagline") or ""
        return f"{n}#{t}" if t else (n or f"P{p['participantId']}")

    # --- Team-Block: fuenf Spieler des eigenen Teams -----------------------
    team_players = []
    for p in parts:
        if p["teamId"] != my_team:
            continue
        pid = p["participantId"]
        role = p.get("teamPosition") or ""
        opp = cmap.get(pid)
        opp_part = by_pid.get(opp) if opp else None
        deltas = analysis.phase_deltas(ser, pid, opp, role)
        ctx = analysis.kill_context(ser["events"]["kills"], pid,
                                    team_kills[my_team])
        deaths = analysis.death_phases(ser["events"]["kills"], pid)
        champ = p.get("championName", "")
        core = core_sets.get(champ, {}).get(role, [])
        items = [p.get(f"item{i}", 0) for i in range(6)]
        sanity = analysis.item_sanity(items, core, id_to_name)
        team_players.append({
            "pid": pid, "champ": champ, "role": role,
            "name": _name(p), "is_me": pid == me_pid,
            "win": bool(p.get("win", False)),
            "counterpart": ({"pid": opp, "champ": opp_part.get("championName"),
                             "name": _name(opp_part)} if opp_part else None),
            "deltas": deltas, "context": ctx, "deaths": deaths,
            "ranking": ranking.get(pid, {}),
            "kda": (p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)),
            "item_sanity": sanity,
        })

    # Reihenfolge (Redesign 2026-07-24): durchgaengig Riot-Rollenreihenfolge
    # TOP->JUNGLE->MIDDLE->BOTTOM->UTILITY; der eigene Spieler wird NICHT mehr
    # vorgezogen, sondern in den Karten per "DU"-Badge hervorgehoben.
    team_players.sort(key=lambda x: analysis.ROLE_ORDER.get(x["role"], 9))

    me_player = next(x for x in team_players if x["is_me"])

    # Endwerte aller 10 fuer das Side-by-side-Scoreboard: Item-Gold = gehaltenes
    # Item-Gold am Spielende (letzter spent-Frame), sonst goldEarned als Fallback.
    sb_finals = {}
    for p in parts:
        pid = p["participantId"]
        spent = ser["players"].get(pid, {}).get("spent", [])
        sb_finals[pid] = {
            "gold": spent[-1] if spent else (p.get("goldEarned", 0) or 0),
            "cs": (p.get("totalMinionsKilled", 0) or 0)
                  + (p.get("neutralMinionsKilled", 0) or 0),
            "vision": p.get("visionScore", 0) or 0,
            "dmg": p.get("totalDamageDealtToChampions", 0) or 0,
            "level": p.get("champLevel", 0) or 0,
            "kda": (p.get("kills", 0) or 0, p.get("deaths", 0) or 0,
                    p.get("assists", 0) or 0),
        }

    # --- Graph-Serien -------------------------------------------------------
    opp_pid = cmap.get(me_pid)
    duo = {}
    for metric in ("spent", "dmg"):
        mine = ser["players"].get(me_pid, {}).get(metric, [])
        opp_s = ser["players"].get(opp_pid, {}).get(metric, []) if opp_pid else []
        duo[metric] = {"me": mine, "opp": opp_s}
    # Vision-Verlauf du vs. Gegenpart (kumulierte Ward-Aktionen).
    duo["vision"] = {
        "me": _cum_ward_series(ser, me_pid),
        "opp": _cum_ward_series(ser, opp_pid) if opp_pid else [],
    }

    tvt = {}
    for metric in ("spent", "dmg"):
        ts = series.team_series(ser, pid_team, metric)
        tvt[metric] = {"me": ts.get(my_team, []),
                       "opp": ts.get(200 if my_team == 100 else 100, [])}
    vis_ts = series.team_vision_series(ser, pid_team)
    tvt["vision"] = {"me": vis_ts.get(my_team, []),
                     "opp": vis_ts.get(200 if my_team == 100 else 100, [])}
    # Team-Kills (key-frei aus dem Kill-Event-Strom) als weiterer Team-vs-Team-Graph.
    kill_ts = series.team_kill_series(ser, pid_team)
    tvt["kills"] = {"me": kill_ts.get(my_team, []),
                    "opp": kill_ts.get(200 if my_team == 100 else 100, [])}
    # Aufsummierte Champion-Level je Team (key-frei; level-Serie in allen Pfaden).
    level_ts = series.team_series(ser, pid_team, "level")
    tvt["level"] = {"me": level_ts.get(my_team, []),
                    "opp": level_ts.get(200 if my_team == 100 else 100, [])}

    # Heuristische Gewinnchance je Minute (key-frei, s. analysis.winprob_series).
    winprob = analysis.winprob_series(
        tvt, elites=ser["events"]["elites"],
        buildings=ser["events"]["buildings"], my_team=my_team)

    # --- Objektive/Elite + Gebaeude ----------------------------------------
    objectives = _objective_summary(ser, my_team, pid_team)

    ranked_names = {p["participantId"]: {"champ": p.get("championName"),
                                         "role": p.get("teamPosition"),
                                         "team": p["teamId"], "name": _name(p)}
                    for p in parts}
    scoreboard = analysis.build_scoreboard(ranked_names, sb_finals, my_team)

    # Composite-Impact-Rohwerte fuer ALLE 10 (Match-Summary liegt vor) - damit
    # der Paar-Vergleich vs. Gegenpart je Rolle geht (§8b Sektion 2). saves aus
    # `challenges` (kann fehlen -> 0), cc_s = timeCCingOthers (Sekunden) -
    # Utility-Erweiterung 2026-07-24, s. analysis.impact_scores.
    impact_raw = {p["participantId"]: {
        "damage": p.get("totalDamageDealtToChampions", 0) or 0,
        "healShield": (p.get("totalHealsOnTeammates", 0) or 0)
                      + (p.get("totalDamageShieldedOnTeammates", 0) or 0),
        "tanked": p.get("damageSelfMitigated", 0) or 0,
        "saves": (p.get("challenges") or {}).get("saveAllyFromDeath", 0) or 0,
        "cc_s": p.get("timeCCingOthers", 0) or 0} for p in parts}

    # Phase-4b-Sektionen: Item-Zeitpunkte aus den Timeline-Kauf-Events.
    seen_by_pid = _seen_from_purchases(ser, id_to_name, id_to_gold)
    extra = _attach_phase4b(
        team_players=team_players, ser=ser, cmap=cmap, pid_team=pid_team,
        my_team=my_team, me_pid=me_pid, opp_pid=opp_pid,
        ranked_names=ranked_names, core_sets=core_sets, seen_by_pid=seen_by_pid,
        impact_raw=impact_raw, has_damage=True, tvt=tvt)

    # Antiheal-Befund (nur der Timeline-Pfad hat die Heilungs-Endwerte der
    # Match-Summary UND die Kauf-Events). Graceful: schlaegt die Static-Abfrage
    # fehl, entfaellt nur die Antiheal-Zeile.
    duration_min = round((info.get("gameDuration", 0) or 0) / 60.0, 1)
    try:
        antiheal = _antiheal_summary(
            parts, ser, my_team, fetch.antiheal_item_ids(cfg, patch),
            id_to_name, duration_min)
    except Exception as exc:   # noqa: BLE001 - Antiheal-Befund ist optional
        log(f"[postgame] Antiheal-Befund uebersprungen ({exc!r}).")
        antiheal = None

    # Auto-Verdikt NACH _attach_phase4b: braucht Teamfights/Impact + die per
    # phase4b angereicherten Team-Karten (Todes-Art, Build-Eval) des me-Spielers.
    verdict = analysis.verdict(
        me_player, win=me_player["win"], outcome_known=True,
        objectives=objectives, teamfights=extra.get("teamfights"),
        impact=extra.get("impact"), scoreboard=scoreboard, has_damage=True,
        team_series=tvt, antiheal=antiheal)

    # Spiel-Ende-Datum (fuer den Trend-Record): gameEndTimestamp, sonst
    # gameCreation + Dauer als Naeherung.
    game_end = info.get("gameEndTimestamp") or (
        (info.get("gameCreation", 0) or 0)
        + (info.get("gameDuration", 0) or 0) * 1000) or None

    report = {
        "match_id": match_id,
        "patch": patch,
        "source": "timeline",     # Datenquelle: Match-V5-Timeline (Key-Pfad)
        "has_damage": True,        # Schaden-an-Champions liegt vor (voller Report)
        "enriched": True,          # Schaden + Impact vorhanden (Renderer/Redesign)
        "outcome_known": True,     # Sieg/Niederlage bekannt (Trend-Record)
        "game_end": game_end,      # Spiel-Ende-Datum (ms) fuer den Trend-Record
        "impact_raw": impact_raw,
        "queue": _QUEUE_NAME.get(info.get("queueId"), str(info.get("queueId"))),
        "duration_min": duration_min,
        "my_team": my_team,
        "win": me_player["win"],
        "me": {"pid": me_pid, "champ": me_player["champ"],
               "role": me_player["role"], "name": me_player["name"],
               "puuid": me_part.get("puuid")},
        "team": team_players,
        "duo_series": duo,
        "team_series": tvt,
        # Ab welcher Minute die Serien gemessen (statt aufgefuellt) sind - im
        # Timeline-Pfad immer 0, weil die Timeline bei Spielbeginn anfaengt.
        "data_start": ser.get("data_start", 0),
        "winprob": winprob,       # heuristische Gewinnchance je Minute (0..1)
        "ranking": ranking,
        "finals": sb_finals,
        "scoreboard": scoreboard,
        "ranked_names": ranked_names,
        "objectives": objectives,
        "verdict": verdict,
        **extra,
    }
    _attach_trend_line(cfg, report, log=log)
    return report


def _cum_ward_series(ser: dict, pid) -> list:
    """Kumulierte Ward-Aktionen (gelegt+zerstoert) eines Spielers je Frame.

    Zaehlt nur echte Ward-Typen (series.is_real_ward) - Riots UNDEFINED-Ward-
    Events (Runen-/Effekt-Wards) wuerden die Vision-Kurve sonst vervielfachen."""
    n = ser["n_frames"]
    buckets = [0] * n
    for w in ser["events"]["wards"]:
        if not series.is_real_ward(w.get("ward_type")):
            continue
        actor = w["creator"] if w["kind"] == "WARD_PLACED" else w["killer"]
        if actor != pid:
            continue
        idx = min(int(w["minute"]), n - 1)
        if idx >= 0:
            buckets[idx] += 1
    run, out = 0, []
    for b in buckets:
        run += b
        out.append(run)
    return out


def _objective_summary(ser: dict, my_team: int,
                       pid_team: dict | None = None) -> dict:
    """Elite-Monster + Tuerme je Team aus den Timeline-Events (Endstand-Zaehlung
    plus Zeitleiste fuer die Todes-/Objective-Analyse).

    Mit `pid_team` kommt zusaetzlich `unconverted` dazu: wie viele Baron-/Elder-
    Buffs je Team folgenlos blieben (s. analysis.unconverted_buffs) - Basis der
    Verdikt-Zeile 'Baron/Elder ohne Folge'."""
    other = 200 if my_team == 100 else 100
    elites = {"me": [], "opp": []}
    for e in ser["events"]["elites"]:
        side = "me" if e["team"] == my_team else "opp"
        elites[side].append({"minute": round(e["minute"], 1),
                             "monster": e["monster"], "subtype": e["subtype"]})
    towers = {"me": 0, "opp": 0}
    for b in ser["events"]["buildings"]:
        if b["building"] == "TOWER_BUILDING":
            # b["team"] ist das Team des zerstoerten Turms -> Gegner bekommt Punkt.
            if b["team"] == my_team:
                towers["opp"] += 1
            elif b["team"] == other:
                towers["me"] += 1
    out = {"elites": elites, "towers": towers}
    if pid_team:
        out["unconverted"] = analysis.unconverted_buffs(
            ser["events"]["elites"], ser["events"]["buildings"],
            ser["events"]["kills"], pid_team, my_team)
    return out


def _antiheal_summary(parts: list, ser: dict, my_team: int,
                      antiheal_ids, id_to_name, duration_min: float) -> dict | None:
    """Gegner-Heilung + eigene Antiheal-Kaeufe (Basis der Antiheal-Verdikt-Zeile).

    Gegner-Heilung = Σ `totalHeal` + `totalHealsOnTeammates` aller gegnerischen
    Spieler (Selbstheilung UND Heilung auf Mitspieler - beides ist das, was das
    eigene Team wegkuerzen muesste). Antiheal-Kaeufe kommen aus dem
    ITEM_PURCHASED-Strom der Timeline, gefiltert ueber die static-basierte
    ID-Menge (s. fetch.antiheal_item_ids). Rueckgabe None, wenn die Daten fehlen
    (dann entfaellt die Zeile ersatzlos)."""
    if not parts or duration_min <= 0:
        return None
    champ_by_pid = {p["participantId"]: p.get("championName") for p in parts}
    total, top = 0, {"champ": None, "heal": 0}
    for p in parts:
        if p.get("teamId") == my_team:
            continue
        heal = ((p.get("totalHeal", 0) or 0)
                + (p.get("totalHealsOnTeammates", 0) or 0))
        total += heal
        if heal > top["heal"]:
            top = {"champ": p.get("championName"), "heal": heal}
    my_pids = {p["participantId"] for p in parts if p.get("teamId") == my_team}
    buys = []
    for pu in ser["events"].get("purchases", []) or []:
        if pu.get("pid") not in my_pids or pu.get("item") not in antiheal_ids:
            continue
        buys.append({"minute": round(pu["minute"], 1),
                     "champ": champ_by_pid.get(pu["pid"]),
                     "item": id_to_name(pu["item"])})
    buys.sort(key=lambda b: b["minute"])
    return {"opp_heal": total, "opp_top": top, "duration_min": duration_min,
            "buys": buys}


# --- Phase 4b: zusaetzliche Statistik-Sektionen (s. plan_postgame.md §8/§8b) --
# Quellenunabhaengige Verdrahtung: beide Report-Pfade (Timeline & Dump/Capture)
# reichen ihre bereits gebauten Primitive herein; die Rechenlogik selbst liegt
# in analysis.py (offline getestet). DD-/KB-abhaengige Teile (Comp-Priors) sind
# defensiv gekapselt - fehlt der Static-Cache, entfaellt nur die Comp-Sektion.

def _comp_champ_info(champs) -> dict | None:
    """{champ: {ad_share, frontline, cc}} aus engine/champions.py fuer die
    Comp-Diagnose. Frontline = Klassen-Bucket 'tank' oder '*_fighter' (Bruiser).
    Jeder Fehlerpfad (kein Static-Cache, Import-/Resolve-Fehler) -> None, sodass
    die key-freie Comp-Sektion sauber entfaellt statt zu crashen."""
    try:
        from engine import champions as ch
        info: dict = {}
        for c in champs:
            cid = ch.resolve_id(c)
            bucket = ch.bucket_for_id(cid)
            info[c] = {
                "ad_share": ch.ad_share_for_id(cid),
                "frontline": bool(bucket and (bucket == "tank"
                                              or bucket.endswith("_fighter"))),
                "cc": ch.cc_per_min_for_id(cid),
            }
        return info
    except Exception:   # noqa: BLE001 - Comp-Sektion ist optional
        return None


def _seen_from_purchases(ser: dict, id_to_name, id_to_gold) -> dict:
    """{pid: {item_name: (erste_minute, gold)}} aus den Timeline-Kauf-Events.
    Erster Kauf des fertigen Items = Fertigstellung (Komponenten fliessen mit,
    werden aber ueber die 'fertig'-Schwelle bzw. Core-Zugehoerigkeit gefiltert)."""
    out: dict = {}
    for pu in ser["events"]["purchases"]:
        pid, name = pu.get("pid"), id_to_name(pu.get("item"))
        if pid is None or not name:
            continue
        d = out.setdefault(pid, {})
        if name not in d:
            d[name] = (pu["minute"], id_to_gold(pu["item"]) if id_to_gold else 0)
    return out


def _seen_from_items_ts(ser: dict, id_to_name, item_gold) -> dict:
    """{pid: {item_name: (erste_minute, gold)}} aus den je-Minute gehaltenen
    Item-IDs des Dumps/Captures (`items_ts`) - der key-freie Ersatz fuer die
    Kauf-Events, die der Live-Client nicht liefert."""
    out: dict = {}
    for pid, s in ser["players"].items():
        d: dict = {}
        for minute, ids in enumerate(s.get("items_ts", []) or []):
            for iid in ids:
                name = id_to_name(iid)
                if not name or name in d:
                    continue
                d[name] = (minute, item_gold(iid) if item_gold else 0)
        out[pid] = d
    return out


def _finished_minutes(seen_pid: dict, core: list) -> list:
    """Aufsteigende Fertigstellungs-Minuten der Fertig-Items eines Spielers
    (Core ODER Gold >= FINISHED_GOLD), aus seiner seen-Map."""
    return sorted(m for _n, (m, g) in
                  ((n, seen_pid[n]) for n in seen_pid)
                  if _n in core or (g or 0) >= analysis.FINISHED_GOLD)


def _build_eval(pid, cmap: dict, seen_by_pid: dict, core_by_pid: dict) -> dict:
    """Build-Eval Stufe 1 (Reihenfolge) + Stufe 2 (Timing vs. Gegenpart) je
    Spieler. `has_core` False -> kein KB-Core (Renderer zeigt dann nichts)."""
    core = core_by_pid.get(pid, [])
    seen = seen_by_pid.get(pid, {})
    finished_named = sorted(
        ((n, m) for n, (m, g) in seen.items()
         if n in core or (g or 0) >= analysis.FINISHED_GOLD),
        key=lambda t: t[1])
    actual_core = [n for n, _m in finished_named if n in core]
    order = analysis.build_order_check(core, actual_core)
    my_min = [m for _n, m in finished_named]
    opp = cmap.get(pid)
    opp_min = _finished_minutes(seen_by_pid.get(opp, {}),
                                core_by_pid.get(opp, [])) if opp else []
    timing = analysis.build_timing_pairs(my_min, opp_min)
    return {"has_core": bool(core), "order": order, "timing": timing}


def _role_dmg_pair(ser: dict, ranked_names: dict, cmap: dict, my_team: int,
                   role: str, n: int) -> dict | None:
    """Schaden-Phasen-Paar (eigener Rolleninhaber vs. Gegenpart) fuer eine Rolle.

    Loest den pid des eigenen Teams mit `role` und dessen counterpart auf und
    baut daraus die Phasen-Zuwaechse (`phase_gain_pairs`, dieselbe Logik wie das
    Me-/Team-Paar). Rueckgabe {role, me_champ, opp_champ, rows} oder None, wenn
    der eigene Rolleninhaber ODER sein Gegenpart fehlt (Renderer laesst das Paar
    dann sauber weg)."""
    me_pid = next((pid for pid, info in ranked_names.items()
                   if info["team"] == my_team and info["role"] == role), None)
    if me_pid is None:
        return None
    opp_pid = cmap.get(me_pid)
    if opp_pid is None:
        return None
    me_dmg = ser["players"].get(me_pid, {}).get("dmg", [])
    opp_dmg = ser["players"].get(opp_pid, {}).get("dmg", [])
    return {"role": role, "me_champ": ranked_names[me_pid]["champ"],
            "opp_champ": ranked_names[opp_pid]["champ"],
            "rows": analysis.phase_gain_pairs(me_dmg, opp_dmg, n)}


def _attach_phase4b(*, team_players: list, ser: dict, cmap: dict,
                    pid_team: dict, my_team: int, me_pid, opp_pid,
                    ranked_names: dict, core_sets: dict, seen_by_pid: dict,
                    impact_raw: dict, has_damage: bool, tvt: dict) -> dict:
    """Baut die zusaetzlichen Top-Level-Sektionen (Schaden-Phasen, Impact, Comp,
    Teamfights) und reichert die Team-Karten an (Build-Eval, Objective-Praesenz,
    Teamfight/Pick-Tode, Jungle-Gank-Zeiten). Rueckgabe = Top-Level-Extras."""
    n = ser["n_frames"]
    kills = ser["events"]["kills"]
    elites = ser["events"]["elites"]
    extra: dict = {}

    # --- Sektion 1: Schaden je Phase (nur mit Schaden-Daten) ----------------
    if has_damage:
        me_dmg = ser["players"].get(me_pid, {}).get("dmg", [])
        opp_dmg = ser["players"].get(opp_pid, {}).get("dmg", []) if opp_pid else []
        # Zusaetzliche Carry-Lane-Paare (ADC=BOTTOM, MID=MIDDLE) - genau die
        # Rollen, fuer die 'hat er early/mid Schaden gemacht?' am relevantesten
        # ist. Eigene Rolle nicht doppeln (steht schon im Me-Paar), fehlender
        # Rolleninhaber/Gegenpart -> Paar weglassen (nicht leer rendern).
        me_role = ranked_names.get(me_pid, {}).get("role")
        role_pairs = []
        for role in ("BOTTOM", "MIDDLE"):
            if role == me_role:
                continue
            pair = _role_dmg_pair(ser, ranked_names, cmap, my_team, role, n)
            if pair:
                role_pairs.append(pair)
        extra["damage_phases"] = {
            "duo": analysis.phase_gain_pairs(me_dmg, opp_dmg, n),
            "role_pairs": role_pairs,
            "team": analysis.phase_gain_pairs(tvt.get("dmg", {}).get("me", []),
                                              tvt.get("dmg", {}).get("opp", []), n),
        }

    # --- Sektion 2: Composite-Impact (nur mit impact_raw) -------------------
    # Die Scores werden hier EINMAL berechnet und weiter unten auch an die
    # UTILITY-Team-Karte durchgereicht (Sup-vs-Sup-Kachel) - kein Nachrechnen.
    imp_scores: dict = {}
    if impact_raw:
        imp_scores = analysis.impact_scores(impact_raw)
        extra["impact"] = {"scores": imp_scores}

    # --- Sektion 3: Comp-Diagnose (key-frei, Static-Cache optional) ---------
    other_team = 200 if my_team == 100 else 100
    my_champs = [i["champ"] for i in ranked_names.values() if i["team"] == my_team]
    opp_champs = [i["champ"] for i in ranked_names.values() if i["team"] == other_team]
    champ_info = _comp_champ_info(my_champs + opp_champs)
    if champ_info is not None:
        extra["comp"] = analysis.comp_diagnosis(
            {"me": my_champs, "opp": opp_champs}, champ_info)
    else:
        extra["comp"] = None

    # --- Sektion 4: Teamfight-Cluster (key-frei) ----------------------------
    # Karten-Modell: je Fight beide Teams getrennt, rollensortiert, Gefallene
    # markiert (analysis.teamfight_cards). Der Kipp-Punkt-Fight wird nur dann
    # dezent markiert, wenn ihn auch das Verdikt nennt - und nur, wenn das Spiel
    # dort laut Team-Serien (`tvt`) wirklich gekippt ist (Korrektur 2026-07-25:
    # ein Stomp hat keinen Kipp-Punkt).
    # Ursachen-Flags der VERLORENEN Fights (Unterzahl-Start, Gold-Rueckstand,
    # gegnerischer Baron-/Elder-Buff, verlorene Eroeffnung) - key-frei aus
    # Kill-Strom, Elite-Events und Team-Serien, also in BEIDEN Datenpfaden
    # verfuegbar. Sie wandern ueber `teamfight_cards` an die Karten und tragen
    # die Warum-Zeile des Verdikts (s. analysis.teamfight_reasons).
    clusters = analysis.detect_teamfights(kills, pid_team, my_team)
    analysis.teamfight_reasons(clusters, kills, pid_team, my_team,
                               elites=elites, team_series=tvt)
    tip_min = analysis.teamfight_tipping_minute(clusters, tvt)
    extra["teamfights"] = analysis.teamfight_cards(
        clusters, ranked_names, my_team, tip_minute=tip_min)

    # --- Sektionen 5-7: Anreicherung der Team-Karten ------------------------
    core_by_pid = {pid: core_sets.get(info["champ"], {}).get(info["role"], [])
                   for pid, info in ranked_names.items()}
    for p in team_players:
        pid = p["pid"]
        p["build_eval"] = _build_eval(pid, cmap, seen_by_pid, core_by_pid)
        # Build-Eval Stufe 3: Engine-Replay (Phase 5, §8b). Graceful - fehlt der
        # Static-Cache/builds.yaml (Schicht 0), faellt die Auswertung sauber aus,
        # statt zu crashen (wie die Comp-Diagnose).
        try:
            p["build_replay"] = build_replay.evaluate_player(
                ser, pid, ranked_names, core_by_pid)
        except Exception:   # noqa: BLE001 - Engine-Replay ist optional
            p["build_replay"] = None
        p["objective"] = analysis.objective_participation(elites, kills, pid,
                                                          my_team)
        p["death_kind"] = analysis.classify_deaths(kills, pid, clusters)
        # Killer-Verteilung der eigenen Tode (Team-Diagnose 2026-07-26): traegt
        # die Zusatz-Aussage "X von Y Toden durch <Champ>" im Verdikt.
        p["death_by"] = analysis.death_killers(
            kills, pid, {q: info["champ"] for q, info in ranked_names.items()})
        # UTILITY-Karte (Erweiterung 2026-07-25): Sup-vs-Sup-Impact-Paar an die
        # Karte durchreichen. Die Karte zeigt sonst nur die Fokusmetrik Vision
        # (ROLE_FOCUS) - der Composite-Impact (Schaden + Heilung/Shield +
        # Getankt + Utility) stand bis dahin nur in der eigenen Sektion. Nur
        # fuer UTILITY: die anderen Rollen haben ihre eigene Fokusmetrik.
        # Ohne impact_raw (key-freier Pfad) bleibt der Schluessel weg -> keine
        # Kachel.
        if p["role"] == "UTILITY" and imp_scores.get(pid):
            opp_sup = cmap.get(pid)
            p["impact_pair"] = {
                "me": imp_scores[pid],
                "opp": imp_scores.get(opp_sup) if opp_sup is not None else None,
                "quote": analysis.impact_quotes(
                    imp_scores, [(pid, opp_sup)]).get(pid),
                # Early/Mid/Late des KOMBINIERTEN Impacts (Schaden + Erlitten +
                # CC·Gewicht - dieselbe Merge-Logik wie im Gesamt-Balken).
                # None, wenn die Minuten-Serien fehlen; Heilung und Saves
                # bleiben aussen vor (nur Match-Endwerte).
                "phase_rows": analysis.impact_phase_rows(ser, pid, opp_sup),
            }
        if p["role"] == "JUNGLE":
            p["gank_times"] = analysis.kill_participation_times(kills, pid)
            # Zweite Strip-Zeile: Kill-Beteiligungen des Rollen-Gegenparts
            # (gegnerischer Jungler) aus demselben Event-Strom - fuer den
            # direkten Aktiv-Zeit-Vergleich. Kein Gegenpart -> None (einzeilig).
            opp_pid_p = cmap.get(pid)
            p["gank_times_opp"] = (
                analysis.kill_participation_times(kills, opp_pid_p)
                if opp_pid_p is not None else None)
    return extra


def _attach_trend_line(cfg: Config, report: dict, *, log=print) -> None:
    """Haengt dem Einzel-Verdikt eine Trend-Zeile an, wenn genug Records derselben
    Identitaet existieren und der Top-Priolisten-Befund auch im aktuellen Spiel
    auftrat (s. trend.verdict_trend_line). Graceful - bricht den Report nie."""
    try:
        line = trend.verdict_trend_line(cfg, report, log=log)
        if line:
            lines = (report.get("verdict") or {}).get("lines")
            if isinstance(lines, list):
                lines.append(line)
    except Exception as exc:   # noqa: BLE001 - Trend-Anbindung ist optional
        log(f"[postgame] Trend-Verdikt-Zeile uebersprungen ({exc!r}).")


def run(cfg: Config, match_id: str, *, me: str | None = None,
        out_dir: Path | None = None, retries: int = 0, backoff: float = 15.0,
        log=print) -> Path:
    """Baut den Report und schreibt `postgame/<matchId>.html`. Gibt den Pfad."""
    report = build_report(cfg, match_id, me=me, retries=retries,
                          backoff=backoff, log=log)
    html = render.render_html(report)
    target_dir = out_dir or cfg.postgame_out_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{match_id}.html"
    path.write_text(html, encoding="utf-8")
    trend.write_record(cfg, report, log=log)
    return path


# --- Key-freier Pfad aus dem Live-Dump (Phase 3, s. plan_postgame.md §2.2b) --

def _resolve_dump_patch(cfg: Config) -> str:
    """Patch fuer den builds.yaml-Item-Sanity-Check aus dem lokalen Cache.

    Der Dump traegt keine gameVersion; fuer das Core-Set (nur elo-unabhaengige
    Item-*Auswahl*) genuegt der neueste lokal vorhandene builds.yaml-Patch. Fehlt
    jeder, gilt der aktuelle Data-Dragon-Patch (Core-Set bleibt dann leer -
    Item-Sanity degradiert sauber)."""
    base = cfg.out_dir
    if base.exists():
        patches = sorted((d.name for d in base.iterdir()
                          if d.is_dir() and (d / "builds.yaml").exists()),
                         reverse=True)
        if patches:
            return patches[0]
    from core import ddragon
    # `latest_version_cached`: der Dump-Pfad ist key-frei und soll auch ohne Netz
    # bis zum Report durchlaufen (Fallback = neueste vollstaendige Cache-Statik).
    return ddragon.patch_of(ddragon.latest_version_cached(cfg.cache_dir))


def _resolve_me_pid_dump(pid_map: dict, ident: str | None) -> int | None:
    """Synthetische pid des eigenen Spielers aus einer Riot-ID/Name.

    `ident` = 'Name#Tag' oder nur 'Name' (Vergleich case-insensitiv gegen den
    riotIdGameName-Teil). None/leer -> None. So wird der activePlayer bzw.
    config `me:` auf die synthetische pid abgebildet."""
    if not ident:
        return None
    name = ident.split("#", 1)[0].strip().lower()
    for p in pid_map["parts"]:
        if (p["riotName"] or "").strip().lower() == name:
            return p["pid"]
    return None


def _patch_vision_deltas(deltas: list, ser: dict, pid: int, opp) -> None:
    """Ersetzt die Vision-Metrik der Phasen-Deltas durch wardScore-Zuwaechse.

    `analysis.phase_deltas` leitet Vision aus Ward-*Events* ab; die hat der Dump
    nicht (nur die kumulative wardScore-Serie je Spieler). Darum wird die
    Vision-Metrik jeder Phase hier nachtraeglich aus der wardScore-Serie gebildet
    (gleiche Phasenfenster wie die Delta-Engine). Gold/CS bleiben unberuehrt."""
    n = ser["n_frames"]
    me_v = ser["players"].get(pid, {}).get("vision", [])
    op_v = ser["players"].get(opp, {}).get("vision", []) if opp is not None else []
    for ph, (_key, _lab, a, b) in zip(deltas, analysis.PHASES):
        mine = analysis._cum_gain(me_v, a, b, n)
        if opp is None:
            ph["metrics"]["vision"] = {"me": mine, "opp": None, "delta": None}
        else:
            other = analysis._cum_gain(op_v, a, b, n)
            ph["metrics"]["vision"] = {"me": mine, "opp": other,
                                       "delta": mine - other}


def _online_report(cfg: Config, pid_map: dict, ident: str | None,
                   log=print) -> dict | None:
    """Vollen Match-V5-Report zum Mitschnitt bauen - oder None (key-frei bleiben).

    Sobald Riot das Spiel indexiert hat, ist Match-V5 die verlaessliche Quelle
    fuer ALLES (Gold/CS/Level/Items/Events/Schaden/Ausgang). Statt einzelne
    Serien in den Live-Report zu mergen, wird darum der komplette Timeline-Report
    gebaut und ersetzt den key-freien (Entscheidung 2026-07-30, WARUM s.
    `enrich`-Modul-Docstring: die Live-Client-API lieferte fuer Viego zeitweise
    leere Item-Listen und damit falsches Item-Gold).

    `enriched_match_id` traegt die echte ID, damit der Trend-Record unter ihr
    laeuft. `source_match_id` setzt der AUFRUFER, wenn eine bestehende Datei-URL
    erhalten bleiben soll (Auto-Report Stufe 2) - der CLI-Dump-Pfad braucht das
    nicht und schreibt direkt unter der echten Match-ID.

    Jeder Fehler (kein Key/Roster-Treffer -> None aus `find_match_id`; Match
    indexiert, Timeline noch 404 -> SystemExit aus `build_report`) endet in None;
    der Aufrufer faellt dann auf den key-freien Report zurueck."""
    try:
        real_id = enrich.find_match_id(cfg, pid_map, ident, log=log)
        if not real_id:
            return None
        report = build_report(cfg, real_id, me=ident, log=log)
    except (Exception, SystemExit) as exc:   # noqa: BLE001 - nie crashen
        log(f"[postgame] Voller Match-Report nicht baubar ({exc!r}) - "
            f"Report bleibt key-frei.")
        return None
    report["enriched_match_id"] = real_id
    log(f"[postgame] Report vollstaendig aus Match {real_id} gebaut.")
    return report


def build_report_from_dump(cfg: Config, dump_dir, *, me: str | None = None,
                           enrich_damage: bool = True,
                           status_override: str | None = None,
                           log=print) -> dict:
    """Report-Modell (Dict) aus einem Live-Dump-Ordner: key-frei - oder, sobald
    das echte Match online ist, der VOLLE Match-V5-Report.

    Ist `enrich_damage` an UND ein gueltiger API-Key da, wird zuerst versucht,
    das zum Dump gehoerende Match zu finden (Roster-Abgleich, s. `enrich`) und
    daraus den vollstaendigen Timeline-Report zu bauen (`source="timeline"`,
    `has_damage=True`, echtes Endergebnis). Klappt das nicht (kein/ungueltiger
    Key, kein Roster-Treffer, Timeline noch nicht indexiert), entsteht wie bisher
    der key-freie Dump-Report: Serien/Events aus dem Live-Client-Capture
    (`live_series`), Gold = Item-Gold (Σ price×count), Vision = wardScore, kein
    Schaden (`has_damage=False`, Disclaimer). `enrich_damage=False` erzwingt den
    key-freien Modus (Tests/Vergleich, `--no-enrich`)."""
    dump_dir = Path(dump_dir)
    snapshots = live_series.load_snapshots(dump_dir)
    mode = live_series.game_mode(snapshots)
    if not live_series.is_supported(snapshots):
        raise SystemExit(
            f"[postgame] Dump-Modus '{mode}' wird nicht ausgewertet - nur "
            f"Summoner's Rift (CLASSIC).")

    pid_map = live_series.build_pid_map(snapshots)
    # Identitaet: --me/config me: > activePlayer-Riot-ID des Dumps.
    ident = (me or cfg.me or "").strip() or live_series.resolve_me_name(snapshots)

    # Erst der volle Online-Report; nur wenn der nicht geht, der key-freie Bau.
    # `source_match_id` bleibt ungesetzt: der CLI-Dump-Pfad hat keine bestehende
    # Datei-URL zu bewahren, `_write_report` legt die HTML unter der echten
    # Match-ID ab.
    if enrich_damage:
        full = _online_report(cfg, pid_map, ident, log=log)
        if full is not None:
            return full

    ser = live_series.build_series_from_dump(snapshots, pid_map)
    finals = live_series.final_stats(snapshots, pid_map)
    id_to_name = live_series.item_name_lookup_from_dump(snapshots)
    item_gold = live_series.item_gold_lookup_from_dump(snapshots)
    duration_min = round(((snapshots[-1].get("gameData", {}) or {})
                          .get("gameTime", 0.0)) / 60.0, 1)
    return _build_report_core(
        cfg, pid_map=pid_map, ser=ser, finals=finals, id_to_name=id_to_name,
        item_gold=item_gold, ident=ident, duration_min=duration_min,
        match_id=dump_dir.name, enrich_damage=enrich_damage,
        status_override=status_override, log=log)


def build_report_from_capture(cfg: Config, result, *, me: str | None = None,
                              enrich_damage: bool = True,
                              status_override: str | None = None,
                              log=print) -> dict:
    """Report-Modell aus einem eingefrorenen In-Memory-Capture (`capture.py`).

    Semantik wie `build_report_from_dump`: `enrich_damage=True` versucht zuerst
    den vollen Match-V5-Report (`_online_report`), `False` erzwingt den key-freien
    Capture-Report. Der zweistufige Server-Auto-Report (Phase 3 Teil C) ruft hier
    IMMER mit `False` (Stufe 1 sofort key-frei, Zwischenstaende ebenso); den
    vollen Report baut Stufe 2 selbst, weil sie die bestehende Datei-URL ueber
    `source_match_id` erhalten muss (s. `app/postgame_watch.py`).

    Die Serien/Events/Endwerte kommen fertig aus dem `CaptureResult`. Identitaet:
    --me/config `me:` > activePlayer aus dem Capture (im Live-Betrieb IST der
    activePlayer der Nutzer)."""
    ident = (me or cfg.me or "").strip() or result.me_ident
    if enrich_damage:
        full = _online_report(cfg, result.pid_map, ident, log=log)
        if full is not None:
            return full
    return _build_report_core(
        cfg, pid_map=result.pid_map, ser=result.ser, finals=dict(result.finals),
        id_to_name=result.id_to_name, item_gold=None, ident=ident,
        duration_min=result.duration_min, match_id=result.match_id,
        queue_label="Summoner's Rift (Live-Capture)",
        enrich_damage=enrich_damage, status_override=status_override, log=log)


def _build_report_core(cfg: Config, *, pid_map: dict, ser: dict, finals: dict,
                       id_to_name, item_gold=None, ident: str | None,
                       duration_min: float, match_id: str,
                       queue_label: str = "Summoner's Rift (Live-Capture)",
                       enrich_damage: bool = True,
                       status_override: str | None = None, log=print) -> dict:
    """Gemeinsamer, KEY-FREIER Report-Kern fuer Datei-Dump UND In-Memory-Capture.

    Bekommt die bereits gebauten Primitive (pid_map, Serien, Endwerte,
    Item-Lookup, Identitaet) und erzeugt daraus das quellenunabhaengige
    Report-Modell. Bewusst ohne jede Online-Anreicherung: sobald das echte Match
    verfuegbar ist, baut `_online_report` stattdessen den VOLLEN Match-V5-Report
    (Entscheidung 2026-07-30) - dieser Kern ist nur noch die Stufe-1-/Fallback-
    Quelle. Folge: kein Schaden, kein Impact, kein Endergebnis (`has_damage`/
    `enriched`/`outcome_known` immer False, `impact_raw` leer). `enrich_damage`
    steuert hier nur noch den Disclaimer-Status (versucht+gescheitert = "failed"
    vs. bewusst abgeschaltet = "disabled")."""
    parts = pid_map["parts"]
    pid_team = pid_map["pid_team"]
    me_pid = _resolve_me_pid_dump(pid_map, ident)
    if me_pid is None:
        me_pid = parts[0]["pid"]
        log("[postgame] Kein 'me' aufloesbar - erstes Team als eigenes gewaehlt.")

    by_pid = {p["pid"]: p for p in parts}
    my_team = by_pid[me_pid]["team"]

    meta_parts = [{"pid": p["pid"], "team": p["team"], "role": p["role"]}
                  for p in parts]
    cmap = analysis.counterpart_map(meta_parts)

    # Team-Kills je Team aus den Endwerten (fuer Kill-Participation).
    team_kills = {100: 0, 200: 0}
    for p in parts:
        team_kills[p["team"]] += finals[p["pid"]]["kills"]

    # Lobby-Ranking ueber alle 10 (Endwerte; dmg=0 -> Schaden-Spalte entfaellt
    # im Renderer bei has_damage=False).
    rank_input = [{"pid": p["pid"], **{k: finals[p["pid"]][k]
                   for k in ("gold", "cs", "dmg", "vision")}} for p in parts]
    ranking = analysis.lobby_ranking(rank_input)

    patch = _resolve_dump_patch(cfg)
    core_sets = _load_core_sets(cfg, patch)

    # --- Team-Block: fuenf Spieler des eigenen Teams -----------------------
    team_players = []
    for p in parts:
        if p["team"] != my_team:
            continue
        pid = p["pid"]
        role = p["role"]
        opp = cmap.get(pid)
        opp_part = by_pid.get(opp) if opp else None
        deltas = analysis.phase_deltas(ser, pid, opp, role)
        _patch_vision_deltas(deltas, ser, pid, opp)
        ctx = analysis.kill_context(ser["events"]["kills"], pid,
                                    team_kills[my_team])
        deaths = analysis.death_phases(ser["events"]["kills"], pid)
        champ = p["champ"]
        core = core_sets.get(champ, {}).get(role, [])
        sanity = analysis.item_sanity(finals[pid]["items"], core, id_to_name)
        f = finals[pid]
        team_players.append({
            "pid": pid, "champ": champ, "role": role,
            "name": p["name"], "is_me": pid == me_pid,
            # Der Live-Mitschnitt kennt kein Endergebnis (Port 2999 liefert es
            # nicht) -> immer False, aber als UNBEKANNT markiert
            # (outcome_known=False weiter unten). Das echte `win` traegt nur der
            # volle Match-V5-Report (`_online_report`).
            "win": False,
            "counterpart": ({"pid": opp, "champ": opp_part["champ"],
                             "name": opp_part["name"]} if opp_part else None),
            "deltas": deltas, "context": ctx, "deaths": deaths,
            "ranking": ranking.get(pid, {}),
            "kda": (f["kills"], f["deaths"], f["assists"]),
            "item_sanity": sanity,
        })

    # Reihenfolge durchgaengig Riot-Standard (Redesign 2026-07-24); eigener
    # Spieler nur per "DU"-Badge markiert, nicht vorgezogen.
    team_players.sort(key=lambda x: analysis.ROLE_ORDER.get(x["role"], 9))
    me_player = next(x for x in team_players if x["is_me"])

    # Endwerte aller 10 fuers Scoreboard: Item-Gold = gehaltenes Item-Gold
    # (finals['gold']), Level = letzter Level-Frame, dmg key-frei immer 0.
    sb_finals = {}
    for p in parts:
        pid = p["pid"]
        f = finals[pid]
        lvl = ser["players"].get(pid, {}).get("level", [])
        sb_finals[pid] = {
            "gold": f["gold"], "cs": f["cs"], "vision": f["vision"],
            "dmg": f.get("dmg", 0), "level": lvl[-1] if lvl else 0,
            "kda": (f["kills"], f["deaths"], f["assists"]),
        }

    # --- Graph-Serien (Gold=Item-Gold, Vision=wardScore; KEIN Schaden) --------
    other_team = 200 if my_team == 100 else 100
    opp_pid = cmap.get(me_pid)
    # Der Live-Mitschnitt hat keinen Schaden an Champions -> die dmg-Serien
    # bleiben leer (Renderer laesst den Schaden-Block weg und zeigt unten den
    # Disclaimer).
    graph_metrics = ["spent", "vision"]
    duo = {}
    for metric in graph_metrics:
        mine = ser["players"].get(me_pid, {}).get(metric, [])
        opp_s = ser["players"].get(opp_pid, {}).get(metric, []) if opp_pid else []
        duo[metric] = {"me": mine, "opp": opp_s}
    duo["dmg"] = {"me": [], "opp": []}

    tvt = {}
    for metric in graph_metrics:
        ts = series.team_series(ser, pid_team, metric)
        tvt[metric] = {"me": ts.get(my_team, []), "opp": ts.get(other_team, [])}
    tvt["dmg"] = {"me": [], "opp": []}
    # Team-Kills (key-frei aus dem Kill-Event-Strom) - in allen Pfaden vorhanden.
    kill_ts = series.team_kill_series(ser, pid_team)
    tvt["kills"] = {"me": kill_ts.get(my_team, []),
                    "opp": kill_ts.get(other_team, [])}
    # Aufsummierte Champion-Level je Team (key-frei; level-Serie in allen Pfaden).
    level_ts = series.team_series(ser, pid_team, "level")
    tvt["level"] = {"me": level_ts.get(my_team, []),
                    "opp": level_ts.get(other_team, [])}

    # Heuristische Gewinnchance je Minute (key-frei - auch der Dump-/Capture-Pfad
    # traegt Team-Gold/Kills/Level und die Objective-Events, s. live_series).
    winprob = analysis.winprob_series(
        tvt, elites=ser["events"]["elites"],
        buildings=ser["events"]["buildings"], my_team=my_team)

    objectives = _objective_summary(ser, my_team, pid_team)
    ranked_names = {p["pid"]: {"champ": p["champ"], "role": p["role"],
                               "team": p["team"], "name": p["name"]}
                    for p in parts}
    scoreboard = analysis.build_scoreboard(ranked_names, sb_finals, my_team)

    # Phase-4b-Sektionen: Item-Zeitpunkte aus den je-Minute gehaltenen Item-IDs
    # (key-frei). `item_gold` fehlt im Capture-Pfad (keine Preise im Speicher) ->
    # 'fertig' faellt dort auf reine Core-Zugehoerigkeit zurueck.
    seen_by_pid = _seen_from_items_ts(ser, id_to_name, item_gold)
    # `impact_raw={}`: Composite-Impact braucht Schaden/Heilung/Getankt aus der
    # Match-Summary - die gibt es nur im vollen Match-V5-Report.
    extra = _attach_phase4b(
        team_players=team_players, ser=ser, cmap=cmap, pid_team=pid_team,
        my_team=my_team, me_pid=me_pid, opp_pid=opp_pid,
        ranked_names=ranked_names, core_sets=core_sets, seen_by_pid=seen_by_pid,
        impact_raw={}, has_damage=False, tvt=tvt)

    # Auto-Verdikt NACH _attach_phase4b (s. build_report). Der Live-/Dump-Pfad
    # kennt kein Endergebnis -> outcome_known=False, damit weder Report noch
    # Trend-Record ein irrefuehrendes "Niederlage" behaupten.
    verdict = analysis.verdict(
        me_player, win=False, outcome_known=False,
        objectives=objectives, teamfights=extra.get("teamfights"),
        impact=extra.get("impact"), scoreboard=scoreboard, has_damage=False,
        team_series=tvt)

    # --- Zustandsbewusster Schaden-Disclaimer (Bugfix 2026-07-24) -----------
    # Der key-freie Report unterscheidet vier Zustaende, damit der Renderer einen
    # EHRLICHEN Hinweis zeigt statt pauschal "kein Key" (realer Vorfall: Dev-Key
    # war da, Stufe 2 scheiterte an Riots langsamer Indexierung, der falsche
    # "kein Key"-Text blieb stehen):
    #   no_key   - wirklich kein Key konfiguriert (active_api_keys leer).
    #   pending  - Key da, der volle Report wird noch nachgeladen (Stufe 1).
    #   failed   - Key da, das Match war nicht auffindbar/baubar (nicht
    #              rechtzeitig indexiert / kein Roster-Treffer).
    #   disabled - Key da, aber --no-enrich erzwingt den key-freien Modus.
    # ("ok" gibt es hier nicht mehr: liegen die Match-Daten vor, kommt der Report
    # komplett aus `build_report` und laeuft gar nicht durch diesen Kern.)
    # `status_override` (pending/failed) setzt der Auto-Report-Watcher; nur er
    # kennt den Kontext (Stufe 1 laeuft / Stufe 2 endgueltig gescheitert). `no_key`
    # gewinnt IMMER (auch ueber einen pending-Override), damit ohne Key nie
    # faelschlich "wird nachgeladen" erscheint.
    if not cfg.active_api_keys:
        damage_status = "no_key"
    elif status_override in ("pending", "failed"):
        damage_status = status_override
    elif enrich_damage:
        damage_status = "failed"
    else:
        damage_status = "disabled"

    report = {
        "match_id": match_id,
        "patch": patch,
        "source": "live_dump",     # Datenquelle: reiner Live-Client-Mitschnitt
        "has_damage": False,       # kein Schaden an Champions -> Disclaimer unten
        "damage_status": damage_status,   # no_key/pending/failed/disabled
        "enriched": False,         # explizites Flag fuer Renderer/Redesign
        # Der Live-/Dump-Pfad kennt kein Endergebnis (win bleibt False, aber
        # unbekannt) -> outcome_known=False, damit der Trend-Record kein
        # irrefuehrendes "Niederlage" speichert.
        "outcome_known": False,
        "game_end": None,          # kein Match-Datum -> Trend nutzt die Erstellzeit
        # Platzhalter fuer die echte Match-ID (Schema-Stabilitaet fuer Trend/
        # History): key-frei ist sie unbekannt, den vollen Report baut
        # `_online_report` und setzt sie dort.
        "enriched_match_id": None,
        "impact_raw": {},          # Composite-Impact nur im vollen Match-Report
        "queue": queue_label,
        "duration_min": duration_min,
        "my_team": my_team,
        "win": False,
        "me": {"pid": me_pid, "champ": me_player["champ"],
               "role": me_player["role"], "name": me_player["name"],
               "puuid": None},
        "team": team_players,
        "duo_series": duo,
        "team_series": tvt,
        # Ab welcher Minute die Serien gemessen (statt aufgefuellt) sind. Startet
        # das Live-Capture erst mitten im Spiel, ist das > 0 und der Renderer
        # graut den Bereich davor als "keine Daten" aus, statt ein erfundenes
        # Plateau ab Minute 0 zu zeigen.
        "data_start": ser.get("data_start", 0),
        "winprob": winprob,       # heuristische Gewinnchance je Minute (0..1)
        "ranking": ranking,
        "finals": sb_finals,
        "scoreboard": scoreboard,
        "ranked_names": ranked_names,
        "objectives": objectives,
        "verdict": verdict,
        **extra,
    }
    _attach_trend_line(cfg, report, log=log)
    return report


def run_from_dump(cfg: Config, dump_dir, *, me: str | None = None,
                  enrich_damage: bool = True, out_dir: Path | None = None,
                  log=print) -> Path:
    """Baut den Dump-Report und schreibt `postgame/<match_id>.html`.

    Wird das echte Match gefunden (Default `enrich_damage=True`), ist das der
    VOLLE Match-V5-Report und die Datei laeuft unter der echten Match-ID; sonst
    der key-freie Dump-Report unter dem Ordnernamen. `enrich_damage=False`
    (`--no-enrich`) erzwingt key-frei."""
    report = build_report_from_dump(cfg, dump_dir, me=me,
                                    enrich_damage=enrich_damage, log=log)
    return _write_report(cfg, report, out_dir)


def run_from_capture(cfg: Config, result, *, me: str | None = None,
                     enrich_damage: bool = True,
                     status_override: str | None = None,
                     out_path: Path | None = None,
                     out_dir: Path | None = None, log=print) -> Path:
    """Baut den Report aus einem In-Memory-Capture und schreibt die HTML-Datei.

    `out_path` erzwingt einen konkreten Pfad - so schreiben Stufe 1 (key-frei)
    und Stufe 2 (voller Match-Report) des Auto-Reports **dieselbe** Datei. Ohne
    `out_path` gilt `postgame/<report-match_id>.html`. `status_override`
    (pending/failed) steuert den zustandsbewussten Disclaimer (Auto-Report:
    Stufe 1 = pending, endgueltiges Scheitern = failed)."""
    report = build_report_from_capture(cfg, result, me=me,
                                       enrich_damage=enrich_damage,
                                       status_override=status_override, log=log)
    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render.render_html(report), encoding="utf-8")
        trend.write_record(cfg, report, log=log)
        return out_path
    return _write_report(cfg, report, out_dir)


def _write_report(cfg: Config, report: dict, out_dir: Path | None) -> Path:
    """Rendert das Report-Modell, schreibt `<out_dir>/<match_id>.html` und
    persistiert zusaetzlich den Trend-Record (postgame/trend/<id>.json)."""
    html = render.render_html(report)
    target_dir = out_dir or cfg.postgame_out_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{report['match_id']}.html"
    path.write_text(html, encoding="utf-8")
    trend.write_record(cfg, report)
    return path
