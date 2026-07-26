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


def replay_candidates(result: dict) -> list[str]:
    """Deterministische Kandidatenliste: [next] + result['items'], dedupliziert."""
    out: list[str] = []
    nxt = result.get("next")
    if nxt and nxt.get("item"):
        out.append(nxt["item"])
    for it in result.get("items", []):
        name = it.get("item")
        if name and name not in out:
            out.append(name)
    return out
