"""Todes-Signal aus dem Kill-Feed (V2-08, plan_engine_v2.md Konzept 3).

Die Live Client Data API liefert unter `/liveclientdata/allgamedata` neben
Spielern und `gameData` auch einen **kumulativen** Event-Strom
(`events.Events[]`, identisch zu `/liveclientdata/eventdata`). Jeder
`ChampionKill`-Eintrag nennt `KillerName`/`VictimName` als *Spielernamen*
(`riotIdGameName`), nicht als Champion - die Zuordnung Name -> Champion liefert
der Aufrufer (`app/server.py` hat sie ohnehin als Identitaets-Pins).

Ausgewertet wird genau eine Frage: **woran sterbe ich?** Ab
`DEATH_SIGNAL_MIN` Toden durch DIESELBE Quelle - denselben Killer-Champion oder
denselben Schadenstyp (AD/AP ueber die Champion-Priors) - schaltet der
Survivability-Layer in `engine/recommend.py` scharf und reserviert einen Slot im
situativen Block fuer eine defensive Option.

Bewusst schweigsam: ohne Event-Daten (alte Dumps, Demo-Modus, Spiel ohne Tode)
liefert `death_signal` `None` - der Layer bleibt dann exakt beim bisherigen
Verhalten, statt aus einer Datenluecke etwas zu behaupten.
"""

from collections import Counter

from . import champions

# Ab wie vielen Toden durch dieselbe Quelle (gleicher Killer-Champion ODER
# gleicher Schadenstyp) das Signal scharf schaltet. Drei, weil zwei Tode noch
# Pech sein koennen und vier zu spaet kommen: nach dem dritten Tod durch
# denselben Gegner ist das ein Muster, kein Zufall - und genau dann ist noch
# genug Spielzeit uebrig, damit ein defensiver Kauf etwas aendert.
DEATH_SIGNAL_MIN = 3

CHAMPION_KILL = "ChampionKill"


def _killer_champion(name: str | None, killers: dict) -> dict | None:
    """Killer-Eintrag ({champion_id, name}) zu einem Event-Namen - oder None.

    Nicht-Champion-Killer (Minion/Turret/Exekution durch Monster) stehen mit
    internen Namen im Feed und tauchen im Lookup nicht auf: sie zaehlen als Tod,
    aber nie als Quelle."""
    if not name:
        return None
    entry = killers.get(name)
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):     # bequeme Kurzform: Name -> champion_id
        return {"champion_id": entry, "name": entry}
    return None


def _damage_type(cid: str | None) -> str | None:
    """'ad' | 'ap' | None fuer EINEN Champion - dieselbe Bucket-Definition wie
    ueberall sonst (`champions.damage_bucket`), damit "AD-Quelle" hier dasselbe
    heisst wie in den by_threat-Zellen. Ohne Prior (unbekannter Champion) oder
    bei 'mixed' gibt es keinen Schadenstyp."""
    share = champions.ad_share_for_id(cid)
    if share is None:
        return None
    bucket = champions.damage_bucket([share])
    return bucket if bucket in ("ad", "ap") else None


def death_signal(events, my_name: str | None, killers: dict,
                 min_deaths: int = DEATH_SIGNAL_MIN) -> dict | None:
    """Todes-Signal oder None (Layer stumm).

    `events`   - rohe `Events[]`-Liste der Live-Client-API (kumulativ).
    `my_name`  - eigener `riotIdGameName` (Victim-Abgleich).
    `killers`  - {Spielername: {champion_id, name}} (oder {Name: champion_id}).

    Rueckgabe bei scharfem Signal:
        {"deaths": <alle eigenen Champion-Tode>,
         "champion": <Anzeigename der dominanten Quelle> | None,
         "champion_id": <Data-Dragon-ID> | None,
         "champion_deaths": <n dieser Quelle>,
         "damage_type": "ad" | "ap" | None,
         "type_deaths": <n dieses Schadenstyps>,
         "trigger": "champion" | "damage_type",
         "reason": "3x von Viego gestorben"}

    Der Champion-Trigger gewinnt gegen den Typ-Trigger: "3x von Viego" ist die
    konkretere (und fuer den Spieler ueberpruefbare) Aussage als "3x an
    AD-Schaden"."""
    if not events or not my_name or min_deaths <= 0:
        return None
    deaths = 0
    by_champ: Counter = Counter()
    by_type: Counter = Counter()
    display: dict[str, str] = {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("EventName") != CHAMPION_KILL:
            continue
        if ev.get("VictimName") != my_name:
            continue
        deaths += 1
        killer = _killer_champion(ev.get("KillerName"), killers)
        if not killer:
            continue
        cid = killer.get("champion_id")
        if not cid:
            continue
        by_champ[cid] += 1
        display[cid] = killer.get("name") or cid
        dmg = _damage_type(cid)
        if dmg:
            by_type[dmg] += 1
    if not deaths:
        return None

    champ_cid, champ_n = (by_champ.most_common(1) or [(None, 0)])[0]
    type_key, type_n = (by_type.most_common(1) or [(None, 0)])[0]
    if champ_n >= min_deaths:
        who = display.get(champ_cid, champ_cid)
        return {"deaths": deaths, "champion": who, "champion_id": champ_cid,
                "champion_deaths": champ_n,
                "damage_type": _damage_type(champ_cid),
                "type_deaths": type_n, "trigger": "champion",
                "reason": f"{champ_n}x von {who} gestorben"}
    if type_n >= min_deaths:
        label = type_key.upper()
        return {"deaths": deaths, "champion": None, "champion_id": None,
                "champion_deaths": champ_n, "damage_type": type_key,
                "type_deaths": type_n, "trigger": "damage_type",
                "reason": f"{type_n}x an {label}-Schaden gestorben"}
    return None


def signal_from_state(data: dict, my_name: str | None, killers: dict,
                      min_deaths: int = DEATH_SIGNAL_MIN) -> dict | None:
    """Bequemer Einstieg fuer den Live-Pfad: nimmt das komplette
    `allgamedata`-Dict und zieht sich den Event-Strom selbst heraus.

    Alte Dumps/Zustaende ohne `events`-Block liefern eine leere Liste - und damit
    None (Layer stumm, kein Fehler)."""
    events = ((data or {}).get("events") or {}).get("Events") or []
    return death_signal(events, my_name, killers, min_deaths)
