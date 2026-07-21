"""Demo-Modus: erzeugt einen realistischen allgamedata-Zustand ohne laufendes
Spiel. Items werden zur Laufzeit per Tags aus dem aktuellen Patch gewaehlt,
damit die IDs immer gueltig sind. Enthaelt bewusst einen Tank-Sion und einen
Crit-Sion-aehnlichen Fall (Jinx) zum Testen der Build-Erkennung.
"""

from . import items


def _pick(want: set[str], avoid: set[str] = frozenset(), count: int = 3) -> list[int]:
    result = []
    for name, (item_id, item) in sorted(items.by_name().items()):
        tags = set(item.get("tags", []))
        gold = item.get("gold", {}).get("total", 0)
        if gold < 2000 or item.get("into") or item.get("requiredAlly"):
            continue
        if not item.get("maps", {}).get("11"):
            continue
        if want & tags and not (avoid & tags):
            result.append(item_id)
        if len(result) >= count:
            break
    return result


def _pick_boots() -> list[int]:
    for name, (item_id, item) in sorted(items.by_name().items()):
        if "Boots" in item.get("tags", []) and item.get("gold", {}).get("total", 0) >= 900:
            return [item_id]
    return []


def _player(champ: str, team: str, item_ids: list[int], k: int, d: int, a: int,
            level: int = 12, summoner: str = "",
            spells: tuple = ("Flash", "Ignite")) -> dict:
    return {
        "championName": champ,
        "summonerName": summoner or f"{champ}Player",
        "riotIdGameName": summoner or f"{champ}Player",
        "team": team,
        "level": level,
        "isDead": False,
        "items": [{"itemID": i, "displayName": items.name_of(i)} for i in item_ids],
        "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": 140},
        "summonerSpells": {
            slot: {"displayName": name,
                   "rawDisplayName": f"GeneratedTip_SummonerSpell_Summoner{name}_DisplayName"}
            for slot, name in zip(("summonerSpellOne", "summonerSpellTwo"), spells)
        },
    }


def fetch_allgamedata() -> dict:
    ap = _pick({"SpellDamage"})
    ad = _pick({"Damage"}, avoid={"SpellDamage"})
    crit = _pick({"CriticalStrike"})
    tank = _pick({"Armor", "SpellBlock"}, avoid={"Damage", "SpellDamage"})
    boots = _pick_boots()

    me = _player("Gwen", "ORDER", ap[:2] + boots, k=2, d=5, a=3, summoner="Nex0r",
                 spells=("Flash", "Smite"))
    return {
        "activePlayer": {"summonerName": "Nex0r", "riotIdGameName": "Nex0r",
                         "level": 12, "currentGold": 2350},
        "allPlayers": [
            me,
            _player("Ornn", "ORDER", tank[:2] + boots, 1, 2, 8),
            _player("Ahri", "ORDER", ap[:2], 4, 3, 5),
            _player("Jinx", "ORDER", crit[:2], 6, 4, 2),
            _player("Thresh", "ORDER", boots, 0, 3, 11, level=10),
            # Gegnerteam: Sion als TANK gebaut (Testfall Build-Erkennung)
            _player("Sion", "CHAOS", tank[:3] + boots, 2, 3, 7, level=14),
            _player("Yasuo", "CHAOS", crit[:3] + boots, 9, 2, 3, level=14,
                    spells=("Flash", "Smite")),
            # Apostroph-Champion (Testfall Frontend-Escaping / Prio-Selector).
            _player("Kha'Zix", "CHAOS", ad[:2] + boots, 5, 4, 6),
            _player("Kaisa", "CHAOS", ad[:2], 3, 5, 4),
            _player("Lulu", "CHAOS", boots, 1, 4, 12, level=10),
        ],
        "events": {"Events": []},
        "gameData": {"gameMode": "CLASSIC", "gameTime": 1420.0},
    }
