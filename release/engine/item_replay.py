"""Geteilte Bausteine fuer Item-Inventar-Replays aus Match-V5-Timelines.

Zwei Aufrufer spielen die Item-Events (`ITEM_PURCHASED/SOLD/DESTROYED/UNDO`)
einer Timeline chronologisch durch und brauchen dieselbe Haertung gegen Riots
Phantom-Destroys:

- der Post-Game-Report `app/postgame/series.py` (`_inventory_ids` -> `items_ts`/
  `spent`) und
- der Offline-Backtest `pipeline/backtest.py` (`replay_match` -> `owned_ids` der
  Samples und die Gegner-Inventare).

`app` und `pipeline` duerfen einander nicht importieren (s.
`tests/test_architecture.py`), der geteilte Baustein gehoert damit in die
Domaenen-Schicht `engine` - genau wie `engine/replay_profile.py`, das aus
demselben Grund aus `pipeline/backtest.py` herausgezogen wurde. Das Modul hat
KEINE Abhaengigkeiten (reine Event-Arithmetik, offline testbar).

Die Erkennung selbst ist eine Diagnose ohne Seiteneffekt: `corrupt_pids` sagt
nur, WELCHE Spieler betroffen sind und welche Event-Bursts einen Kauf enthalten -
die Reparatur-Regel ("Destroy nur im Kauf-Burst zaehlt") wendet jeder Aufrufer in
seinem eigenen Replay an, weil sich die Inventare (erste vs. letzte Instanz,
Snapshot-Zeitpunkte) unterscheiden.
"""


def remove_one(lst: list, item) -> None:
    """Entfernt die erste Instanz von `item` aus `lst` (in place), falls
    vorhanden. Ein Verkauf/Destroy/Undo trifft genau EINE Instanz, nicht alle
    Stacks."""
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


def phantom_destroy_counts(frames: list, pids=None) -> dict:
    """Je participantId: Zahl der ITEM_DESTROYED-Events auf einem Item, das der
    Spieler zu diesem Zeitpunkt gar nicht haelt (nie gekauft bzw. schon wieder
    weg). Erwerb = ITEM_PURCHASED oder ITEM_UNDO-`afterId` (wiederhergestellter
    Verkauf); Abgang = SOLD/DESTROYED/UNDO-`beforeId`.

    `pids`: optionale Einschraenkung auf bekannte Spieler (sonst alle).
    Diagnose-Funktion ohne Seiteneffekt - Basis fuer `corrupt_pids`."""
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
                    remove_one(held.setdefault(pid, []), before)
                if after:
                    held.setdefault(pid, []).append(after)
            elif et == "ITEM_SOLD":
                remove_one(held.setdefault(pid, []), ev.get("itemId"))
            elif et == "ITEM_DESTROYED":
                bag = held.setdefault(pid, [])
                out.setdefault(pid, 0)
                iid = ev.get("itemId")
                if iid in bag:
                    bag.remove(iid)
                else:
                    out[pid] += 1
    return out


def item_bursts(frames: list, pids=None) -> dict:
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


def corrupt_pids(frames: list, pids=None) -> tuple[set, set]:
    """(korrumpierte pids, (pid, ts)-Paare mit Kauf) - s. Modul-Kommentar oben.

    Korrumpiert ist ein Spieler, wenn er sowohl einen No-Kauf-Burst mit >=
    CORRUPT_BURST_DESTROYS Destroys hat ALS AUCH >= PHANTOM_DESTROY_LIMIT
    Phantom-Destroys. Fuer alle anderen bleibt das Replay unveraendert
    (Paritaets-Garantie fuer saubere Timelines).

    Die Kauf-Stempel fallen im selben Durchgang an und werden mitgegeben, damit
    das Reparatur-Replay die Timeline nicht ein drittes Mal durchlaufen muss."""
    bursts = item_bursts(frames, pids)
    buy_stamps = {key for key, (buys, _k) in bursts.items() if buys}
    big = {pid for (pid, _ts), (buys, kills) in bursts.items()
           if not buys and kills >= CORRUPT_BURST_DESTROYS}
    if not big:
        return set(), buy_stamps
    phantoms = phantom_destroy_counts(frames, big)
    return ({pid for pid in big
             if phantoms.get(pid, 0) >= PHANTOM_DESTROY_LIMIT}, buy_stamps)
