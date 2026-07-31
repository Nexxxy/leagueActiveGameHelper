"""Geteilte Replay-Adapter fuer Engine-Replays (Offline-Backtest + Post-Game).

Diese beiden Funktionen lagen urspruenglich als `_enemy_profile`/`_candidates` in
`pipeline/backtest.py`. Zwei Aufrufer brauchen sie:

- der Offline-Backtest `pipeline/backtest.py` (Crawler-System) und
- der Post-Game-Report `app/postgame/build_replay.py`, Build-Eval Stufe 3
  (Spieler-System).

Beide liegen in Paketen, die einander nicht importieren duerfen - der geteilte
Adapter gehoert damit in die Domaenen-Schicht `engine`, auf die beide zugreifen.
Einzige Abhaengigkeit ist `engine.profiling`; die Crawl-Kette `backtest` ->
`aggregate` -> `matchindex` wird nicht nachgezogen, was das schlanke App-Release
(`release-base-tool.sh` paketiert nur `app`/`engine`/`core`) erst moeglich macht.

Verhalten ist 1:1 identisch zu vorher - reine Verschiebung, keine Logikaenderung.
"""

from . import profiling


def enemy_profile(pid: int, inv: dict, kda: dict, cs: dict, level: dict,
                  pid_meta: dict) -> dict:
    """Live-API-foermiges Gegner-Profil aus dem simulierten Zustand."""
    _team, pos, champ, _win = pid_meta[pid]
    k = kda[pid]
    player = {
        "championName": champ,
        "level": level.get(pid, 0),
        "scores": {"kills": k["kills"], "deaths": k["deaths"],
                   "assists": k["assists"], "creepScore": cs.get(pid, 0)},
        "items": [{"itemID": iid} for iid in inv[pid]],
    }
    prof = profiling.profile_player(player)
    prof["role"] = pos          # recommend nutzt e.get("role") fuer den Lane-Lead
    return prof


def replay_candidates(result: dict, *, exclude_boots: bool = False) -> list[str]:
    """Deterministische Kandidatenliste: [next] + result['items'], dedupliziert.

    `exclude_boots=True` laesst ALLE Boots-Empfehlungen weg (`kind == "boots"`,
    sowohl `next` als auch Eintraege in `items`).

    WARUM (Befund 1, plan_engine_v2.md §1): die Engine liefert Boots und
    regulaere Items in EINER Liste. Wird ein regulaerer Fertig-Kauf gegen die
    Top-3 dieser Liste gemessen, belegen Boots-Vorschlaege regelmaessig zwei der
    drei Slots (`next` = Boots + ein zweiter Boots-Eintrag) - echte Core-Items
    fallen hinten raus und der Kauf zaehlt als Abweichung, obwohl er KB-konform
    ist (real: Jhin 0/4 bei Top-3 "Steelcaps / Collector / Swiftness"). Boots
    haben in beiden Aufrufern (`app/postgame/build_replay.py`,
    `pipeline/backtest.py`) ihre EIGENE Bewertung gegen die Boots-Teilmenge der
    Engine; sie duerfen die Item-Top-3 deshalb nicht mitbelegen.
    """
    out: list[str] = []
    nxt = result.get("next")
    if nxt and nxt.get("item") and not (exclude_boots and nxt.get("kind") == "boots"):
        out.append(nxt["item"])
    for it in result.get("items", []):
        if exclude_boots and it.get("kind") == "boots":
            continue
        name = it.get("item")
        if name and name not in out:
            out.append(name)
    return out
