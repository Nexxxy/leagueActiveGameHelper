"""Optionale Schaden-Anreicherung des key-freien Dump-Reports (Phase 3 Teil B).

Der Live-Dump liefert alles ausser **Schaden an Champions** (s. plan_postgame.md
2.2b). Ist ein gueltiger API-Key da, wird das zum Dump gehoerende Match + seine
Timeline nachgeladen und die fehlenden Schaden-Daten geliefert:

  * **Schaden-ueber-Zeit** je Spieler aus der Timeline
    (`participantFrames[*].damageStats.totalDamageDoneToChampions`) sowie -
    fuer die Impact-Phasen-Balken - **erlittener Schaden** (`totalDamageTaken`)
    und **CC-Zeit** (`timeEnemySpentControlled`, s. TIMELINE_SERIES),
  * **Schaden-Endwert** je Spieler (fuer die Lobby-Ranking-Spalte),
  * **Composite-Impact-Rohwerte** je Spieler (Schaden / Heilung+Shield /
    getankter Schaden) fuer den spaeteren Impact-Score (§8).

Alles ist auf die **synthetischen Dump-pids** (101–105/201–205) gemappt, damit
die anreichernde Serie zu den uebrigen Dump-Serien passt. **Jeder Fehlerpfad
(kein Key, kein Roster-Treffer, Fetch/401/403, Timeline fehlt) endet in `None`**
- der Aufrufer bleibt dann sauber key-frei (Disclaimer, kein Crash).

Reine Anreicherung; Netz nur beim Fetch (Cache-first). Der RiotClient wird ueber
den Seam `_region_client` bezogen, damit Tests ihn ohne Netz mocken koennen.
"""

from core import ddragon
from core.cacheio import write_json
from core.config import Config
from . import fetch, series

# Timeline-Serien, die auf die Dump-pids gemappt werden (Erweiterung
# 2026-07-25). `dmg` traegt die Schaden-Graphen/-Deltas, `dmg_taken` (roher
# erlittener Schaden) und `cc_s` (CC-Zeit in Sekunden) die Phasen-Balken der
# Sup-vs-Sup-Impact-Kachel. Heilung/Shield und Saves liegen NICHT je Minute vor
# (nur als Match-Endwert) - dafuer gibt es bewusst keine Serie.
TIMELINE_SERIES = ("dmg", "dmg_taken", "cc_s")


def _region_client(cfg: Config):
    """RiotClient der Heimatregion (eigener Seam, in Tests gemockt).

    Das Dump-Spiel liegt in der eigenen Region des Nutzers - dieselbe wie fuer
    `--latest`. Delegiert an `fetch._region_client`."""
    return fetch._region_client(cfg)


def _load_match(cfg: Config, client, match_id: str):
    """Match Cache-first laden, sonst ueber den Client (und cachen). None bei 404.

    Cache-Layout wie der Rest der Pipeline (`matches/<patch>/<id>.json`); ein
    frisch geholtes Match wird gleich abgelegt, damit ein zweiter Report-Lauf
    offline bleibt."""
    _, cached = fetch._find_cached(cfg, "matches", match_id)
    if cached is not None:
        return cached
    match = client.match(match_id)
    if match is None:
        return None
    patch = ddragon.patch_of(match["info"].get("gameVersion", ""))
    write_json(cfg.cache_dir / "matches" / patch / f"{match_id}.json", match)
    return match


def _load_timeline(cfg: Config, client, match_id: str, patch: str):
    """Timeline Cache-first laden, sonst ueber den Client (und cachen). None bei 404."""
    _, cached = fetch._find_cached(cfg, "timelines", match_id)
    if cached is not None:
        return cached
    timeline = client.match_timeline(match_id)
    if timeline is None:
        return None
    write_json(cfg.cache_dir / "timelines" / patch / f"{match_id}.json", timeline)
    return timeline


def _roster_matches(dump_champs: frozenset, match: dict) -> bool:
    """True, wenn die Champion-Menge des Matches exakt der des Dumps entspricht.

    Robuster Zuordnungs-Test (statt naiv 'neuestes Spiel'): ein Dump kann ein
    aelteres Spiel sein - nur bei identischer 10er-Champion-Menge ist es sicher
    dasselbe Match (Champions sind pro SR-Spiel eindeutig)."""
    parts = (match.get("info", {}) or {}).get("participants", []) or []
    match_champs = frozenset(p.get("championName", "") for p in parts
                             if p.get("championName"))
    return bool(dump_champs) and match_champs == dump_champs


def _tl_to_dump_pid(match: dict, pid_map: dict) -> dict:
    """Timeline/Match-participantId (1–10) -> synthetische Dump-pid.

    Abbildung ueber `riotIdGameName` (Fallback `championName`), damit die
    Schaden-Serien den richtigen Dump-Spielern zugeordnet werden - der Dump nutzt
    andere pids (101–105/201–205) als die Timeline (1–10)."""
    name_to_pid = pid_map["name_to_pid"]
    champ_to_pid = {p["champ"]: p["pid"] for p in pid_map["parts"] if p["champ"]}
    out: dict[int, int] = {}
    for p in (match.get("info", {}) or {}).get("participants", []) or []:
        tl_pid = p.get("participantId")
        dpid = (name_to_pid.get(p.get("riotIdGameName"))
                or champ_to_pid.get(p.get("championName")))
        if tl_pid is not None and dpid is not None:
            out[tl_pid] = dpid
    return out


def fetch_damage_enrichment(cfg: Config, snapshots: list, pid_map: dict,
                            me_ident: str | None, *, lookback: int = 10,
                            log=print):
    """Sucht Match+Timeline zum Dump und liefert die Schaden-Anreicherung.

    Ablauf: Identitaet -> PUUID (account-v1) -> die letzten `lookback` Match-IDs
    (ungefiltert, ranked UND normal) -> je Kandidat Match Cache-first laden und
    den mit **identischer Champion-Menge** waehlen (Roster-Match). Danach Timeline
    laden, Schaden-Serien + Impact-Rohwerte extrahieren und auf die Dump-pids
    mappen.

    Rueckgabe bei Erfolg::

        {"match_id": str,
         "dmg_series": {dump_pid: [kum. Schaden je Minute]},
         "series":     {"dmg"|"dmg_taken"|"cc_s": {dump_pid: [je Minute]}},
         "final_dmg":  {dump_pid: int},
         "impact_raw": {dump_pid: {damage, healShield, tanked}}}

    `dmg_series` bleibt als Kurzform des Schaden-Eintrags erhalten (identisches
    Objekt wie `series["dmg"]`); `series` traegt zusaetzlich `dmg_taken` und
    `cc_s` fuer die Phasen-Balken der Sup-vs-Sup-Impact-Kachel.

    Bei JEDEM Fehlerpfad (kein Key, keine Identitaet, kein Roster-Treffer,
    Fetch/Timeline fehlt) -> `None`; der Aufrufer bleibt dann key-frei."""
    if not cfg.active_api_keys:
        log("[postgame] Kein API-Key - Report bleibt key-frei (kein Schaden).")
        return None
    ident = (me_ident or "").strip()
    if not ident:
        log("[postgame] Keine Identitaet fuer die Schaden-Anreicherung - key-frei.")
        return None

    client = _region_client(cfg)
    puuid = fetch._resolve_puuid(client, ident, log=log)
    if not puuid:
        log(f"[postgame] Identitaet '{ident}' nicht aufloesbar - key-frei.")
        return None

    ids = client.match_ids(puuid, queue=None, count=lookback,
                           type_filter=None) or []
    if not ids:
        log("[postgame] Keine Match-IDs fuer die Anreicherung gefunden - key-frei.")
        return None

    dump_champs = frozenset(p["champ"] for p in pid_map["parts"] if p["champ"])
    match = None
    match_id = None
    for mid in ids:
        cand = _load_match(cfg, client, mid)
        if cand is not None and _roster_matches(dump_champs, cand):
            match, match_id = cand, mid
            break
    if match is None:
        log("[postgame] Kein Match mit passendem Roster gefunden - key-frei.")
        return None

    patch = ddragon.patch_of(match["info"].get("gameVersion", ""))
    timeline = _load_timeline(cfg, client, match_id, patch)
    if timeline is None:
        log(f"[postgame] Timeline zu {match_id} nicht abrufbar - key-frei.")
        return None

    tl_to_dump = _tl_to_dump_pid(match, pid_map)
    tl_ser = series.build_series(timeline)

    # Timeline-Serien auf die Dump-pids mappen. Neben dem Schaden (Graphen,
    # Delta-Engine) auch `dmg_taken` (roher erlittener Schaden) und `cc_s`
    # (CC-Zeit in Sekunden) - beide speisen die Phasen-Balken der Sup-vs-Sup-
    # Impact-Kachel (Erweiterung 2026-07-25), die sonst nur im Timeline-Pfad
    # Daten haette.
    mapped: dict[str, dict] = {k: {} for k in TIMELINE_SERIES}
    for tl_pid, dpid in tl_to_dump.items():
        pser = tl_ser["players"].get(tl_pid, {})
        for key in TIMELINE_SERIES:
            mapped[key][dpid] = list(pser.get(key, []))

    # Endwert (Ranking-Spalte) + Composite-Impact-Rohwerte aus der Match-Summary.
    # "getankter Schaden" = damageSelfMitigated: das ist der ueber Ruestung/MR/
    # Shields tatsaechlich abgefangene Schaden und damit das fairere Tank-/Front-
    # line-Mass als das rohe totalDamageTaken (das auch durchgegangenen Schaden
    # zaehlt) - passt zum Support/Tank-Impact-Score aus §8 (Brand-vs-Soraka).
    final_dmg: dict[int, int] = {}
    impact_raw: dict[int, dict] = {}
    for p in (match.get("info", {}) or {}).get("participants", []) or []:
        dpid = tl_to_dump.get(p.get("participantId"))
        if dpid is None:
            continue
        dmg = p.get("totalDamageDealtToChampions", 0) or 0
        final_dmg[dpid] = dmg
        # Utility-Rohwerte (Erweiterung 2026-07-24): `saveAllyFromDeath` liegt in
        # `challenges` (kann fehlen -> 0), `timeCCingOthers` ist ein Top-Level-Feld
        # (Sekunden). Verrechnung + Konstanten in analysis.impact_scores.
        chal = p.get("challenges") or {}
        impact_raw[dpid] = {
            "damage": dmg,
            "healShield": (p.get("totalHealsOnTeammates", 0) or 0)
                          + (p.get("totalDamageShieldedOnTeammates", 0) or 0),
            "tanked": p.get("damageSelfMitigated", 0) or 0,
            "saves": chal.get("saveAllyFromDeath", 0) or 0,
            "cc_s": p.get("timeCCingOthers", 0) or 0,
        }

    return {"match_id": match_id, "dmg_series": mapped["dmg"],
            "series": mapped, "final_dmg": final_dmg, "impact_raw": impact_raw}


def fit_series(seq: list, n: int) -> list:
    """Bringt eine (kumulative) Serie auf Laenge `n` (Dump-Frames = Minuten).

    Kuerzere Serie wird mit dem letzten Wert aufgefuellt (kumulativer Schaden
    steigt monoton, der Endwert bleibt gueltig), laengere wird abgeschnitten. So
    passt die Timeline-Schaden-Serie exakt zu den uebrigen Dump-Serien (gleiche
    n_frames -> `analysis._cum_gain`/`series.team_series` bleiben sauber)."""
    if n <= 0:
        return []
    if not seq:
        return [0] * n
    last = seq[-1]
    return [seq[i] if i < len(seq) else last for i in range(n)]
