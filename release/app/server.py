"""FastAPI-Server: liefert den aufbereiteten Spielzustand ans Frontend,
schreibt currentGameInfo.yaml und nimmt Prio-/Rollen-Overrides entgegen.

Start:
  python -m app.server            (echtes Spiel via Live Client Data API)
  python -m app.server --demo     (synthetischer Zustand zum Testen)
"""

import argparse
import json
import threading
import time
from datetime import datetime

import yaml
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import ROOT, VALID_ROLES, Config
from engine import items, knowledge, profiling, rec_partner, recommend
from . import assets, demo, live_client

app = FastAPI(title="League Active Game Helper")
CFG = Config.load()

# Laufender Post-Game-Watcher (nur im Voll-Modus gesetzt, s.
# _start_postgame_watch). /api/state liest daraus den letzten fertigen Report;
# ohne Watcher (Demo/Trigger aus) bleibt das Feld null.
_WATCHER = None

STATE = {
    "demo": False,
    "priorities": {},      # championName -> low/medium/high/urgent
    "role_override": None, # z.B. "JUNGLE"
    "cache": (0.0, None),  # (timestamp, state)
    "last_game": None,     # letzter In-Game-Zustand, bleibt nach Spielende stehen
    "dump": False,             # Live-Data-Dumping aktiv (--dumplivedata / config)
    "dump_dir": None,          # aktueller Spiel-Unterordner in liveGameData/
    "dump_last_gametime": None,  # letzte gesehene gameTime (Reset-Erkennung)
    "identity": {},            # riotId -> {champion_id, name} (Befund B, pro Spiel fix)
    "last_gametime": None,     # letzte gesehene gameTime (Identitaets-Reset)
    "fed_state": {},           # riotId -> {"strong": bool, "weak": bool} (Befund C, Hysterese)
}

# Serialisiert Zugriffe auf _build_state (Frontend-Handler + Hintergrund-Poller).
_STATE_LOCK = threading.Lock()

# Zielverzeichnis fuer archivierte Live-Snapshots (Befund I).
DUMP_ROOT = ROOT / "liveGameData"


# Genau eine Quelle fuer die Rollen-Reihenfolge (dupliziert nicht mehr
# core.config.VALID_ROLES). Lokaler Name ROLE_ORDER bleibt fuer die
# Konsumenten unveraendert.
ROLE_ORDER = VALID_ROLES


def _roles_from_order(players: list[dict]) -> dict[int, str]:
    """Rollen aus der Scoreboard-Reihenfolge (Tab-Menue): In Ranked/Draft
    listet die Live-API jedes Team in der Reihenfolge TOP,JGL,MID,BOT,SUP.
    Plausibilitaetscheck pro Team: jeder per _detect_role erkannte Spieler
    (Smite -> JUNGLE, Support-Item -> UTILITY) muss auf dem zu SEINER erkannten
    Rolle passenden Platz sitzen, sonst wird die Zuordnung fuer dieses Team
    verworfen (z.B. Blind Pick ohne Rollen-Sortierung).
    Rueckgabe: Index im allPlayers-Array -> Rolle."""
    teams: dict[str, list[int]] = {}
    for idx, player in enumerate(players):
        teams.setdefault(player.get("team", "?"), []).append(idx)
    result: dict[int, str] = {}
    for indices in teams.values():
        if len(indices) != len(ROLE_ORDER):
            continue
        mapping = dict(zip(indices, ROLE_ORDER))
        if all(mapping[i] == role for i in indices
               if (role := _detect_role(players[i])) is not None):
            result.update(mapping)
    return result


def _holds_support_item(player: dict) -> bool:
    """True, wenn der Spieler ein Item der Support-Item-Linie traegt. Marker sind
    die Data-Dragon-Tags 'GoldPer' UND 'Lane' GEMEINSAM - das trifft exakt die
    Support-Item-Linie (World Atlas, Spellthief's, Steel Shoulderguards, Spectral
    Sickle samt Mid-/Endstufen) und keine anderen Items (Stormsurge traegt nur
    'GoldPer', Consumables 'GoldPer'+'Consumable'). Patch-stabil ohne Namensliste."""
    for entry in player.get("items", []):
        item = items.by_id(entry.get("itemID"))
        if item and {"GoldPer", "Lane"} <= set(item.get("tags", [])):
            return True
    return False


def _detect_role(player: dict) -> str | None:
    """Per-Spieler-Rollensignal aus der Live-API: Smite im Spell-Setup -> JUNGLE,
    sonst ein getragenes Support-Item -> UTILITY, sonst None. Beide sind starke
    Rollen-Marker (Jungler tragen Smite, Supports das Support-Item). Smite hat
    Vorrang: ein Jungler mit versehentlichem GoldPer-Item bleibt JUNGLE.
    rawDisplayName ist locale-unabhaengig ('...SummonerSmite...'), displayName
    als Fallback."""
    for spell in player.get("summonerSpells", {}).values():
        if not isinstance(spell, dict):
            continue
        raw = f"{spell.get('rawDisplayName', '')} {spell.get('displayName', '')}"
        if "smite" in raw.lower():
            return "JUNGLE"
    if _holds_support_item(player):
        return "UTILITY"
    return None


def _role_from_kb(champion: str) -> str:
    """Fallback-Rolle aus der Wissensbasis: die meistgespielte Rolle des
    Champions. Schwaecher als Reihenfolge/Smite, aber besser als gar kein
    Signal - v.a. damit die Vorsprungs-Anzeige (rec_stance.fielded_lead) den
    direkten Gegenpart findet, wenn Scoreboard-Reihenfolge und Smite fehlen."""
    if not champion:
        return ""
    return knowledge.for_champion(champion)[0] or ""


def _rid(player: dict) -> str:
    """Stabile riotId eines Spielers (riotIdGameName#riotIdTagLine, Fallback
    summonerName). Einzige ueber ein Spiel stabile Kennung (Befund B) - genutzt
    fuer Identitaets-Pin und Fed-Hysterese-Zustand (Befund C)."""
    game_name = player.get("riotIdGameName")
    tag = player.get("riotIdTagLine")
    if game_name:
        return f"{game_name}#{tag}" if tag else game_name
    return player.get("summonerName", "")


def _resolve_players(players: list[dict],
                     identity: dict) -> list[tuple[dict, dict]]:
    """Spieler-Identität an der riotId festmachen (Befund B, 2026-07-15).

    Neekos Passiv aendert ihre Identität IN der Live-Client-API (sie erschien im
    selben Spiel als Neeko, Caitlyn und sogar als Minion). Stabil blieb nur die
    riotId. Deshalb wird die Champion-Identität pro riotId bei der ERSTEN
    verifizierten Sichtung gepinnt und danach unveraendert weiterverwendet - in
    Ranked/Draft kann sich der echte Champion eines Spielers nie aendern, spaetere
    Verkleidungen prallen ab.

    Nicht auflösbare Eintraege OHNE bekannte Identität (Minions in allPlayers,
    deren rawChampionName keinen Champion ergibt) werden VERWORFEN statt geraten.

    `identity` (riotId -> {champion_id, name}) wird in-place gepflegt. Rueckgabe:
    Liste (player, pin) der behaltenen Spieler - der Aufrufer arbeitet ab hier mit
    genau dieser gefilterten Liste (Index-Konsistenz fuer Rollen/me/Gegner).

    Bekannte Grenze: startet der Server, WAEHREND Neeko gerade verkleidet ist,
    wird die Verkleidung gepinnt. Verwandlungen dauern nur Sekunden - akzeptiert."""
    resolved: list[tuple[dict, dict]] = []
    for player in players:
        rid = _rid(player)
        pin = identity.get(rid)
        if pin is None:
            cid = profiling.verified_champion_id(player)
            if cid is None:
                continue  # Nicht-Champion ohne bekannte Identität -> verwerfen
            pin = {"champion_id": cid, "name": player.get("championName", cid)}
            if rid:
                identity[rid] = pin
        resolved.append((player, pin))
    return resolved


def _apply_fed_hysteresis(profile: dict, rid: str, fed_state: dict,
                          game_time: float) -> None:
    """Fed-Flag-Hysterese ueber Snapshots halten (Befund C).

    Der Vorschritt-Zustand pro riotId wird ins Profil gespiegelt
    (`fed_prev_strong`/`fed_prev_weak`), damit is_strongly_fed/is_fed_enough
    getrennte Auslöse-/Loslass-Schwellen anwenden koennen (sonst blinkt das Flag,
    weil die Erwartung waechst, waehrend der Gegner gerade nichts kauft). Danach
    wird der neu berechnete Zustand in `fed_state` zurueckgeschrieben. recommend()
    bleibt unveraendert - es ruft dieselben deterministischen Funktionen auf
    demselben Profil auf und kommt zwangslaeufig zum selben Ergebnis."""
    prev = fed_state.get(rid, {})
    profile["fed_prev_strong"] = prev.get("strong", False)
    profile["fed_prev_weak"] = prev.get("weak", False)
    fed_state[rid] = {
        "strong": profiling.is_strongly_fed(profile, game_time),
        "weak": profiling.is_fed_enough(profile, game_time),
    }


def _add_item_ids(reco: dict) -> None:
    """Reichert Empfehlungen (items + next) um die numerische item_id an, damit
    das Frontend das passende Data-Dragon-Icon laden kann. Unbekannte Namen
    bleiben ohne Feld. recommend.py bleibt bewusst unberuehrt (Backtest)."""
    lookup = items.by_name()
    for entry in reco.get("items", []):
        found = lookup.get(entry.get("item"))
        if found:
            entry["item_id"] = found[0]
    nxt = reco.get("next")
    if nxt:
        found = lookup.get(nxt.get("item"))
        if found:
            nxt["item_id"] = found[0]


def _dump_live_snapshot(raw: dict, computed: dict) -> None:
    """Archiviert einen Live-Poll als JSON (raw + computed) unter liveGameData/.

    Pro Spiel ein eigener Unterordner (Wallclock-Timestamp + gameMode), damit
    mehrere Spiele einer Server-Session sich nicht ueberschreiben. Ein neues
    Spiel wird erkannt, wenn noch kein Ordner aktiv ist oder die gameTime
    deutlich zurueckspringt (Reset). Darf den Server nie crashen -> caller
    kapselt in try/except."""
    game_time = float(raw.get("gameData", {}).get("gameTime", 0) or 0)
    last_gt = STATE["dump_last_gametime"]
    new_game = (STATE["dump_dir"] is None
                or (last_gt is not None and game_time < last_gt - 30))
    if new_game:
        mode = raw.get("gameData", {}).get("gameMode") or "unknown"
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        game_dir = DUMP_ROOT / f"{stamp}__{mode}"
        game_dir.mkdir(parents=True, exist_ok=True)
        STATE["dump_dir"] = game_dir
    STATE["dump_last_gametime"] = game_time

    snapshot = {
        "dumped_at": datetime.now().isoformat(),
        "raw": raw,
        "computed": computed,
    }
    fname = f"snapshot_t{int(round(game_time)):07d}.json"
    out = STATE["dump_dir"] / fname
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                   encoding="utf-8")


def _build_state() -> dict:
    fetch = demo.fetch_allgamedata if STATE["demo"] else live_client.fetch_allgamedata
    data = fetch()
    if data is None:
        # Spiel vorbei: aktiven Dump-Ordner zuruecksetzen, damit das naechste
        # Spiel einen frischen Unterordner bekommt.
        STATE["dump_dir"] = None
        STATE["dump_last_gametime"] = None
        # Spiel vorbei: Identitaets-Pins zuruecksetzen (Befund B), damit das
        # naechste Spiel frisch pinnt.
        STATE["identity"] = {}
        STATE["last_gametime"] = None
        STATE["fed_state"] = {}
        # Spiel vorbei: letzten Stand als post_game weiter anzeigen, bis das
        # naechste Spiel laedt (dann antwortet die Live-API wieder mit Daten).
        last = STATE["last_game"]
        if last is not None:
            if last.get("phase") != "post_game":
                last = {**last, "phase": "post_game"}
                STATE["last_game"] = last
                _export_yaml(last)
            return last
        return {"phase": "no_game", "patch": knowledge.load()["patch"],
                "patch_inherited_from": knowledge.load()["inherited_from"]}

    # Neues Spiel erkennen (gameTime springt deutlich zurueck): Identitaets-Pins
    # zuruecksetzen, damit Champions des neuen Spiels frisch gepinnt werden
    # (gleiches Reset-Muster wie beim Live-Data-Dump).
    game_time = float(data.get("gameData", {}).get("gameTime", 0) or 0)
    last_gt = STATE["last_gametime"]
    if last_gt is not None and game_time < last_gt - 30:
        STATE["identity"] = {}
        STATE["fed_state"] = {}
    STATE["last_gametime"] = game_time

    active_name = (data.get("activePlayer", {}).get("riotIdGameName")
                   or data.get("activePlayer", {}).get("summonerName", ""))
    # Identität pro riotId pinnen und Nicht-Champions (Minions) verwerfen (Befund
    # B). Ab hier wird ausschliesslich mit der gefilterten Liste gearbeitet -
    # Rollen, me-Suche und alle Index-Zugriffe muessen konsistent bleiben.
    resolved = _resolve_players(data.get("allPlayers", []), STATE["identity"])
    players = [p for p, _ in resolved]
    pins = {id(p): pin for p, pin in resolved}
    me = next((p for p in players
               if (p.get("riotIdGameName") or p.get("summonerName")) == active_name),
              players[0])
    my_team = me.get("team")
    order_roles = _roles_from_order(players)

    enemies = []
    for idx, player in enumerate(players):
        if player.get("team") == my_team:
            continue
        pin = pins[id(player)]
        profile = profiling.profile_player(
            player, champion_id=pin["champion_id"], name=pin["name"])
        profile["role"] = (order_roles.get(idx) or _detect_role(player)
                           or _role_from_kb(pin["name"]))
        _apply_fed_hysteresis(profile, _rid(player), STATE["fed_state"], game_time)
        enemies.append(profile)
    profiling.add_threat_scores(enemies, STATE["priorities"])
    # Anzeige-Ranking nach display_score (Prio-gewichtet, Fix 5.2) - die manuelle
    # Priority hebt eine Karte nach oben, ohne die Stance zu beeinflussen.
    enemies.sort(key=lambda p: p["display_score"], reverse=True)

    my_pin = pins[id(me)]
    my_profile = profiling.profile_player(
        me, champion_id=my_pin["champion_id"], name=my_pin["name"])
    owned = set(my_profile["items"])
    owned_ids = [entry["itemID"] for entry in me.get("items", [])]
    # Item-Namen der Mitspieler (fuer die Anti-Heal-Team-Coverage): hat schon
    # ein Ally Grievous Wounds, muss der Spieler es nicht selbst bauen.
    ally_items = set()
    ally_gold_spent = 0
    # Profil des eigenen BOTTOM-Partners fuer die Partner-Kontext-Achse (nur fuer
    # UTILITY relevant, s.u.). Rolle des Allys analog zur Gegner-Logik bestimmen:
    # Scoreboard-Reihenfolge > Smite > Wissensbasis. Index muss konsistent zur
    # gefilterten players-Liste sein (enumerate), sonst zeigt order_roles daneben.
    bottom_ally_profile = None
    for idx, player in enumerate(players):
        if player is me or player.get("team") != my_team:
            continue
        ap = pins[id(player)]
        ally_profile = profiling.profile_player(
            player, champion_id=ap["champion_id"], name=ap["name"])
        ally_items.update(ally_profile["items"])
        # Fuer den Team-Kontext der Vorsprungs-Anzeige (rec_stance.lead_note).
        ally_gold_spent += ally_profile["gold_spent"]
        ally_role = (order_roles.get(idx) or _detect_role(player)
                     or _role_from_kb(ap["name"]))
        if ally_role == "BOTTOM":
            bottom_ally_profile = ally_profile
    # Prioritaet: manueller Override > Smite (eindeutig) > Scoreboard-Position
    role_hint = (STATE["role_override"] or _detect_role(me)
                 or order_roles.get(players.index(me)))
    # Partner-Kontext (research_bot_sup_mates.md 9.40): NUR wenn der Spieler
    # selbst UTILITY ist, den Bot-Partner klassifizieren und durchreichen. Fuer
    # jede andere Rolle bleibt bot_partner None -> striktes No-Op fuer den Pool.
    bot_partner = None
    if role_hint == "UTILITY" and bottom_ally_profile is not None:
        partner_class = rec_partner.classify_partner(
            bottom_ally_profile["champion_id"], bottom_ally_profile)
        bot_partner = {**bottom_ally_profile, "partner_class": partner_class}
    reco = recommend.recommend(
        my_pin["name"], role_hint, owned,
        my_profile["scores"], enemies,
        game_time=data.get("gameData", {}).get("gameTime", 0),
        current_gold=data.get("activePlayer", {}).get("currentGold"),
        owned_ids=owned_ids,
        my_level=my_profile.get("level", 0),
        ally_items=ally_items,
        champion_id=my_profile["champion_id"],
        ally_gold_spent=ally_gold_spent,
        bot_partner=bot_partner,
    )
    _add_item_ids(reco)

    state = {
        "phase": "in_game",
        "patch": knowledge.load()["patch"],
        # Aus welchem Patch geerbte Eintraege stammen (None = alles eigene Daten
        # des aktuellen Patches). Macht sichtbar, dass frisch nach einem
        # Patch-Wechsel noch Vorpatch-Wissen ausgeliefert wird.
        "patch_inherited_from": knowledge.load()["inherited_from"],
        "game_time": data.get("gameData", {}).get("gameTime", 0),
        # Erkannte/gesetzte Rolle anzeigen, auch wenn die Wissensbasis fuer
        # sie keine Daten hat und die Empfehlungen auf eine andere ausweichen
        "player": {**my_profile, "role": role_hint or reco["role"]},
        "enemies": enemies,
        "recommendations": reco,
    }
    # Partner-Klasse fuer Frontend/YAML sichtbar machen - nur wenn ueberhaupt
    # klassifiziert wurde (UTILITY + Bot-Partner erkannt). Feld-Abwesenheit ist
    # das No-Op-Signal fuer alle anderen Rollen, darum bei None NICHT setzen.
    if bot_partner is not None:
        state["player"]["bot_partner"] = {
            "champion": bot_partner["champion_id"],
            "class": bot_partner["partner_class"],
        }
    STATE["last_game"] = state
    _export_yaml(state)
    if STATE["dump"] and not STATE["demo"]:
        try:
            _dump_live_snapshot(data, state)
        except Exception as exc:  # Dumping darf den Server nie crashen
            print(f"Warnung: Live-Data-Dump fehlgeschlagen ({exc})")
    return state


def _export_yaml(state: dict) -> None:
    out = ROOT / "currentGameInfo.yaml"
    out.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")


@app.get("/api/state")
def get_state():
    ts, cached = STATE["cache"]
    if cached is None or time.monotonic() - ts >= 2.0:
        with _STATE_LOCK:
            cached = _build_state()
        STATE["cache"] = (time.monotonic(), cached)
    # refresh_seconds steuert das Auto-Reload-Intervall des Frontends (config.yml)
    return {**cached, "refresh_seconds": CFG.refresh_seconds,
            "assets_available": assets.assets_available(),
            "postgame_report": _postgame_report()}


def _postgame_report():
    """Letzter fertiger Post-Game-Report fuer den Frontend-Button, oder None.

    Bewusst NICHT gecacht (steht ausserhalb des STATE["cache"]-Snapshots), damit
    ein waehrend der 2 s Cache-Fenster fertig gewordener Report sofort sichtbar
    wird. Ohne Watcher (Demo/Trigger aus) oder wenn die Datei fehlt -> None."""
    if _WATCHER is None:
        return None
    return _WATCHER.last_report()


class PriorityUpdate(BaseModel):
    champion: str
    priority: str  # low | medium | high | urgent


@app.post("/api/priority")
def set_priority(update: PriorityUpdate):
    STATE["priorities"][update.champion] = update.priority
    STATE["cache"] = (0.0, None)
    return {"ok": True}


class RoleUpdate(BaseModel):
    role: str | None  # TOP | JUNGLE | MIDDLE | BOTTOM | UTILITY | null


@app.post("/api/role")
def set_role(update: RoleUpdate):
    STATE["role_override"] = update.role
    STATE["cache"] = (0.0, None)
    return {"ok": True}


# Post-Game-Reports als statische Dateien ausliefern (der Frontend-Button
# verlinkt auf /reports/<datei>). Ordner bei Start anlegen, sonst wirft
# StaticFiles beim Mount. WICHTIG: dieser Mount MUSS vor dem Catch-all "/"
# stehen, sonst schluckt das Frontend-Mount die /reports-Pfade. Fehlende
# Einzeldateien liefern einen harmlosen 404 (kein 500).
CFG.postgame_out_dir.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=CFG.postgame_out_dir), name="reports")

app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")


def _dump_poll_once() -> None:
    """Ein einzelner Poller-Durchlauf: _build_state unter dem Lock ausfuehren.
    Exceptions werden abgefangen, damit der Poller-Thread nie terminiert."""
    try:
        with _STATE_LOCK:
            _build_state()
    except Exception as exc:
        print(f"Warnung: Dump-Poller-Fehler ({exc})")


def _dump_poll_loop() -> None:
    """Endlosschleife: ruft in fester Kadenz _dump_poll_once auf, unabhaengig
    vom Frontend-Polling."""
    while True:
        _dump_poll_once()
        time.sleep(CFG.dump_interval_seconds)


def _postgame_poll_loop(watcher) -> None:
    """Endlosschleife: pollt die Live Client Data API in fester Kadenz und reicht
    die Rohdaten (`allgamedata` oder None) an den Watcher weiter. Aktive Polls
    fuellen das In-Memory-Capture; die Transition 'aktiv -> kein Spiel' triggert
    den zweistufigen Auto-Report des gerade beendeten Spiels. Darf den Server nie
    crashen -> jeder Durchlauf in try/except."""
    while True:
        try:
            watcher.observe(live_client.fetch_allgamedata())
        except Exception as exc:  # Auto-Trigger darf den Server nie crashen
            print(f"Warnung: Post-Game-Auto-Trigger-Fehler ({exc})")
        time.sleep(CFG.postgame_poll_interval_seconds)


def _start_postgame_watch(demo: bool) -> None:
    """Startet den Post-Game-Auto-Trigger (Phase 3 Teil C) als Daemon-Thread,
    sofern das Flag `postgame.auto_on_end` gesetzt ist und kein Demo-Modus laeuft.
    Key/`me:` sind KEINE Voraussetzung mehr - ohne Key entsteht der key-freie
    Stufe-1-Report trotzdem. Ist das Flag aus (oder Demo), bleibt es ein stiller
    No-Op mit genau EINER erklaerenden Log-Zeile."""
    global _WATCHER
    from .postgame_watch import PostgameWatcher
    watcher = PostgameWatcher(CFG, log=print, demo=demo)
    if not watcher.enabled:
        print(f"Post-Game-Auto-Report deaktiviert ({watcher.disabled_reason}).")
        return
    # Watcher fuer /api/state sichtbar machen (Report-Button-Zustand).
    _WATCHER = watcher
    print(f"Post-Game-Auto-Report aktiv (Spielende-Erkennung alle "
          f"{CFG.postgame_poll_interval_seconds}s) ...")
    threading.Thread(target=_postgame_poll_loop, args=(watcher,),
                     daemon=True).start()


def _start_asset_download() -> None:
    """Startet den Item-Icon-Download in einem Daemon-Thread, damit der
    Serverstart nicht auf einen (moeglicherweise langsamen) Download von
    ~700 Icons wartet. Netz-/IO-Fehler duerfen den Hintergrund-Thread nicht
    crashen -> nur eine Warnung. assets_available() scannt den Ordner pro
    /api/state-Aufruf, daher flippt die Frontend-Anzeige automatisch auf
    'verfuegbar', sobald die ersten Icons geschrieben sind."""
    def _load_assets() -> None:
        try:
            assets.download_missing()
        except Exception as exc:  # Kein Internet o.ae. darf den Start nicht blocken
            print(f"Warnung: Item-Icons konnten nicht geladen werden ({exc})")
    threading.Thread(target=_load_assets, daemon=True).start()


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="synthetische Daten statt Live-Spiel")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dumplivedata", action="store_true",
                        help="Live-Rohdaten + berechnete Zustaende nach liveGameData/ archivieren")
    args = parser.parse_args()
    STATE["demo"] = args.demo
    STATE["dump"] = args.dumplivedata or CFG.dump_live_data
    if CFG.auto_asset_download:
        print("Item-Icons werden im Hintergrund geladen ...")
        _start_asset_download()
    if STATE["dump"] and not args.demo:
        print(f"Live-Data-Dump aktiv -> {DUMP_ROOT}")
        print(f"Dump-Poller aktiv, alle {CFG.dump_interval_seconds}s")
        threading.Thread(target=_dump_poll_loop, daemon=True).start()
    _start_postgame_watch(args.demo)
    print(f"http://127.0.0.1:{args.port}  (Demo-Modus: {'an' if args.demo else 'aus'})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
