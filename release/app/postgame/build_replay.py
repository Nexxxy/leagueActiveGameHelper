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

**Top-3-Definition:** identisch zum Backtest (`replay_candidates(result)[:3]` =
`[next]` + `result["items"]`, dedupliziert). Ein regulaerer Fertig-Kauf ist ein
"Hit", wenn er in diesen Top-3 liegt.

**Boots:** die Engine hat eine eigene Boots-Logik (CC-lastiges Team -> Tenacity,
sonst AD/AP-Konter). Boots-Kaeufe werden mitbewertet, aber in einer EIGENEN
Teilmenge gezaehlt (gegen die Boots-Kandidaten der Engine, nicht die allgemeinen
Top-3) - so entscheidet ein Mercs-vs-Steelcaps-Streit nicht ueber den Item-Score.

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


# --- Haupt-Auswertung je Spieler --------------------------------------------

def evaluate_player(ser: dict, pid, ranked_names: dict, core_by_pid: dict,
                    *, weights=None) -> dict:
    """Engine-Replay-Auswertung fuer EINEN Team-Spieler.

    Rueckgabe bei bewertbarem Spieler:
      {evaluable: True, score:{hits,total}, boots:{hits,total},
       purchases:[{minute,item,kind,hit,engine_top:[...]}, ...]}
    Sonst: {evaluable: False, reason: "..."}.

    `core_by_pid`: {pid: [Core-Item-Namen]} (fuer die Fertig-Erkennung; auch die
    Gegner-Cores werden hier nur zur Item-Klassifikation gebraucht - der Engine-
    Input selbst kommt aus dem rekonstruierten Zustand)."""
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

    purchases = []
    hits = total = bhits = btotal = 0
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

            if kind == "boots":
                # Boots gegen die EIGENE Boots-Logik der Engine messen (eigene
                # Teilmenge) - unabhaengig von den allgemeinen Top-3.
                engine_boots = [r["item"] for r in result.get("items", [])
                                if r.get("kind") == "boots"]
                nxt = result.get("next")
                if nxt and nxt.get("kind") == "boots" and nxt["item"] not in engine_boots:
                    engine_boots.insert(0, nxt["item"])
                hit = name in engine_boots
                btotal += 1
                bhits += int(hit)
                top = engine_boots[:3]
            else:
                nxt = result.get("next")
                if not (nxt and nxt.get("item")):
                    # Engine hat fuer diesen Zustand nichts zu sagen (Build
                    # komplett / nur Elixier) -> nicht wertbar, ueberspringen.
                    continue
                cands = replay_candidates(result)
                top = cands[:3]
                hit = name in top
                total += 1
                hits += int(hit)
            purchases.append({"minute": m, "item": name, "kind": kind,
                              "hit": hit, "engine_top": top})

    return {"evaluable": True,
            "score": {"hits": hits, "total": total},
            "boots": {"hits": bhits, "total": btotal},
            "purchases": purchases}
