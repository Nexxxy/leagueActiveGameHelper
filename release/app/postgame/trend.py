"""Trend-Aggregation ueber N Games (Post-Game-Report Phase 6, s.
docu/plan_postgame.md §6 Phase 6 + §2.5).

Grundidee (§2.5): Ein Einzelspiel ist verrauscht (der Gegenpart kann Smurf/Int
sein) und dient als Story. Der **Trend ueber N Games** ist die eigentliche,
rauschfreie Uebungsliste - die Gegenpart-Deltas werden ueber Spiele aggregiert
und nach **Konsistenz x Schwere** des Rueckstands sortiert (Prioliste).

Bausteine (alle offline/logik-pur, kein Netz):
  extract_record(report)      -> kompakter Per-Game-Extrakt der EIGENEN Werte
  write_record(cfg, report)   -> persistiert nach postgame/trend/<id>.json
  load_records(cfg, ident)    -> Records der Identitaet (Filter Name/PUUID)
  aggregate(records, n)       -> Prioliste + Rollen-/Champion-Splits + Meta
  top_finding(agg)            -> Top-Priolisten-Befund (fuer die Verdikt-Zeile)
  occurs_in_record(finding,r) -> trat der Befund auch im aktuellen Spiel auf?

Der Aggregations-Ausgang wird von `render.render_trend_html` in eine
self-contained HTML-Seite im Report-Design uebersetzt.
"""

import time
from pathlib import Path

from core.cacheio import read_json, write_json

# --- Konstanten / Schwellen (tunebar) ---------------------------------------

# Nur Metriken mit >= so vielen auswertbaren Spielen kommen in die Prioliste
# (Rausch-Disziplin, §2.5 - unter 3 Spielen ist der Trend nicht belastbar).
MIN_TREND_GAMES = 3

# Default-Fenstergroesse fuer `--trend` (die N neuesten Records).
DEFAULT_N = 10

# >= so viele Tode vor Minute 10 zaehlen als "frueher Tod" in einem Spiel
# (deckungsgleich mit analysis.EARLY_DEATH_MIN).
EARLY_DEATH_MIN = 3

# Impact-Quote unter diesem Wert = Rueckstand zum Rollen-Gegenpart.
QUOTE_BEHIND = 1.0

# Normierungs-Skalen der Schwere je Metrik: ein mittlerer Rueckstand in dieser
# Groesse zaehlt als volle Schwere (severity = 1.0). Bewusst grobe, dokumentierte
# Referenzwerte (elo-unabhaengig gemeint - es geht nur um die RELATIVE Ordnung
# der Metriken untereinander, nicht um absolute Kalibrierung):
#   Gold  3000 = ein ganzes Kern-Item Rueckstand
#   CS      40 = ~2 Wellen/Minute-Rueckstand ueber das Spiel
#   Vision  20 = deutlicher Ward-Rueckstand
#   Schaden 8000 = spuerbarer Teamfight-Schaden-Rueckstand
DELTA_SCALE = {"gold": 3000.0, "cs": 40.0, "vision": 20.0, "dmg": 8000.0}

# Anzeige-Labels der Prioliste (nur Metrik-/Befund-Namen, keine Spielernamen).
METRIC_LABEL = {"gold": "Item-Gold-Rückstand", "cs": "CS-Rückstand",
                "vision": "Vision-Rückstand", "dmg": "Schaden-Rückstand"}
QUOTE_LABEL = "Impact unter Gegenpart"
EARLY_DEATH_LABEL = "Frühe Tode (vor Min 10)"


# ============================================================================
# 1. Record-Extraktion aus dem Report-Modell
# ============================================================================

def _me_card(report: dict) -> dict | None:
    """Team-Karte des eigenen Spielers (is_me) aus dem Report-Modell."""
    for p in report.get("team", []) or []:
        if p.get("is_me"):
            return p
    return None


def _total_deltas(me_card: dict, has_damage: bool) -> dict:
    """Gesamt-Delta je Metrik vs. Gegenpart ueber alle Phasen (Σ Phasen-Deltas).

    Da die Phasen-Deltas Zuwaechse zusammenhaengender Fenster (Early/Mid/Late)
    sind, ist ihre Summe der Gesamt-Delta des Spiels. `None`, wenn kein Gegenpart
    (delta None) - dann ist die Metrik in diesem Spiel nicht auswertbar. Schaden
    nur bei `has_damage`."""
    out: dict[str, float | None] = {}
    for m in ("gold", "cs", "vision", "dmg"):
        # Schaden bleibt key-frei leer (None) - Schema stabil ueber alle Records.
        if m == "dmg" and not has_damage:
            out[m] = None
            continue
        total = 0.0
        seen = False
        for ph in me_card.get("deltas", []) or []:
            d = (ph.get("metrics", {}).get(m) or {}).get("delta")
            if d is None:
                total = None
                break
            total += d
            seen = True
        out[m] = total if seen else None
    return out


def _impact_quote(report: dict) -> float | None:
    """Rollen-faire Impact-Quote des eigenen Spielers (eigener/Gegenpart-Impact).

    Nutzt `impact.scores` + die Scoreboard-Paarung (dieselbe Quelle wie das
    Einzel-Verdikt). `None`, wenn kein Impact/Schaden vorliegt oder der Gegenpart
    fehlt."""
    impact = report.get("impact") or {}
    scores = impact.get("scores") or {}
    me_pid = (report.get("me") or {}).get("pid")
    if not scores or me_pid is None:
        return None
    # Gegenpart-pid ueber die Scoreboard-Zeile des eigenen Spielers.
    opp_pid = None
    for row in report.get("scoreboard", []) or []:
        me_side = row.get("me") or {}
        if me_side.get("pid") == me_pid:
            opp_pid = (row.get("opp") or {}).get("pid")
            break
    me_s = scores.get(me_pid)
    opp_s = scores.get(opp_pid) if opp_pid is not None else None
    if not me_s or not opp_s:
        return None
    opp_total = opp_s.get("total", 0) or 0
    if not opp_total:
        return None
    return (me_s.get("total", 0) or 0) / opp_total


def _build_summary(me_card: dict) -> dict:
    """Build-Eval-Kompakt: mittlerer Timing-Gap (min hinter Gegenpart) +
    Engine-Score (hits/total). Fehlende Teile -> None."""
    be = me_card.get("build_eval") or {}
    gaps = [(t.get("mine") - t.get("opp")) for t in (be.get("timing") or [])
            if t.get("opp") is not None and t.get("mine") is not None]
    gap = round(sum(gaps) / len(gaps), 2) if gaps else None
    replay = me_card.get("build_replay") or {}
    score = replay.get("score") if replay.get("evaluable") else None
    hits = score.get("hits") if score else None
    total = score.get("total") if score else None
    return {"timing_gap_min": gap, "engine_hits": hits, "engine_total": total}


def extract_record(report: dict) -> dict:
    """Kompakter Per-Game-Extrakt der EIGENEN Werte aus einem Report-Modell.

    Enthaelt Identitaet (voller Name + PUUID falls vorhanden), Champion/Rolle/
    Queue/Datum/Sieg, die Gegenpart-Gesamt-Deltas je Metrik, Impact-Quote,
    Todes-Muster (early/teamfight/pick), Build-Eval (Timing-Gap, Engine-Score),
    Objective-Praesenz und `has_damage`. Key-gebundene Felder bleiben leer/None,
    wenn der Report key-frei ist (die Aggregation ueberspringt sie).

    Zusaetzlich fuer die Match-History-Seite (2026-07-29): `damage_status`
    (Daten-Qualitaet des Reports) und `roster` (die 10 Champions als
    Wiederfinde-Schluessel fuer den Retry). Aeltere Records ohne diese Felder
    bleiben gueltig - alle Leser nutzen `.get()`.

    Die Record-ID ist die echte Match-ID, sobald bekannt (`enriched_match_id` aus
    dem Capture-Enrichment), sonst `match_id` (im Capture-Pfad der `live_<...>`-
    Stempel)."""
    me = report.get("me") or {}
    me_card = _me_card(report) or {}
    has_damage = bool(report.get("has_damage"))
    deaths = me_card.get("deaths") or {}
    death_kind = me_card.get("death_kind") or {}
    obj = me_card.get("objective") or {}

    record_id = report.get("enriched_match_id") or report.get("match_id")
    date_ms = report.get("game_end")
    if not date_ms:
        date_ms = int(time.time() * 1000)

    # Roster (alle 10 Champions) fuer den Retry der Match-History-Seite: nach
    # einem Server-Neustart ist das In-Memory-Capture weg, der Record ist dann
    # die einzige Quelle, um das Spiel per Roster-Match in der Match-History
    # wiederzufinden (s. app/history.py).
    roster = [(v or {}).get("champ") for v in
              (report.get("ranked_names") or {}).values()]
    roster = [c for c in roster if c]

    return {
        "match_id": record_id,
        # Stempel, unter dem die HTML-Datei liegt. Normalerweise die eigene
        # `match_id`; der History-Retry (app/history.py) baut den vollen Report
        # ueber die ECHTE ID, behaelt aber die vorhandene `live_<...>.html` -
        # dann traegt er den Datei-Stempel hier explizit ein, damit der Record
        # weiter zu dieser Datei gehoert (und der stale Stempel-Record unten
        # verschwindet).
        "source_match_id": report.get("source_match_id") or report.get("match_id"),
        "name": me.get("name"),
        "puuid": me.get("puuid"),
        "champ": me.get("champ"),
        "role": me.get("role"),
        "queue": report.get("queue"),
        "date_ms": int(date_ms),
        "win": report.get("win") if report.get("outcome_known") else None,
        "has_damage": has_damage,
        # Daten-Qualitaet des Reports (ok/pending/failed/no_key/disabled) fuer
        # die Match-History-Seite. Der Timeline-Pfad setzt kein `damage_status`
        # (dort liegt der Schaden per Definition vor) -> "ok" ableiten.
        "damage_status": (report.get("damage_status")
                          or ("ok" if has_damage else None)),
        "roster": roster,
        "deltas": _total_deltas(me_card, has_damage),
        # Impact-Quote ist key-gebunden (Schaden/Heilung/Tank) -> key-frei leer.
        "impact_quote": _impact_quote(report) if has_damage else None,
        "deaths": {
            "early": int(deaths.get("early", 0) or 0),
            "teamfight": int(death_kind.get("teamfight", 0) or 0),
            "pick": int(death_kind.get("pick", 0) or 0),
        },
        "build": _build_summary(me_card),
        "objective": {"present": int(obj.get("present", 0) or 0),
                      "total": int(obj.get("total", 0) or 0)},
    }


# ============================================================================
# 2. Persistenz (postgame/trend/<id>.json)
# ============================================================================

def trend_dir(cfg) -> Path:
    """Ordner der Trend-Records (unter dem gitignorten postgame/-Output)."""
    return cfg.postgame_out_dir / "trend"


def _drop_displaced_stamp(cfg, match_id: str, new_stamp, *, log=print) -> None:
    """Die vom neuen Record VERDRAENGTE Stempel-Datei entfernen.

    WARUM (Bugfix 2026-07-30, realer Vorfall): Es gibt genau EINEN Record je
    Spiel (`<match_id>.json`), und sein `source_match_id` sagt, welche HTML-Datei
    dazugehoert. Baut jemand dasselbe Spiel unter einem ANDEREN Stempel neu -
    typischerweise `pipeline postgame <match_id>`, das den Record mit
    `source_match_id == match_id` schreibt -, verliert die bisher verlinkte
    `live_<...>.html` ihren Record ersatzlos. Sie bleibt als tote
    "unbekannt"-Zeile in der Match-History liegen; ihr Retry fand ohne Record
    weder Roster noch Datum und meldete ewig "nicht gefunden".

    Deshalb raeumt der Record-Write die verdraengte Datei (und deren etwaigen
    Stamm-Record) gleich mit weg - die Invariante "genau EINE Datei je Spiel"
    gilt damit auch fuer den CLI-Pfad, nicht nur fuer den History-Retry.
    Aufraeum-Fehler werden nur geloggt: sie duerfen den Record-Write nie
    brechen."""
    current = trend_dir(cfg) / f"{match_id}.json"
    if not current.exists():
        return
    old = read_json(current)
    old_stamp = old.get("source_match_id") if isinstance(old, dict) else None
    if not old_stamp or old_stamp == new_stamp or old_stamp == match_id:
        return
    old_html = cfg.postgame_out_dir / f"{old_stamp}.html"
    if not old_html.exists():
        return
    try:
        old_html.unlink()
        log(f"[postgame] Verdraengte Report-Datei {old_html.name} entfernt "
            f"(Spiel {match_id} liegt jetzt unter {new_stamp}).")
    except OSError as exc:
        log(f"[postgame] Verdraengte Report-Datei {old_html.name} nicht "
            f"loeschbar ({exc!r}).")
        return
    stale = trend_dir(cfg) / f"{old_stamp}.json"
    try:
        if stale.exists():
            stale.unlink()
    except OSError as exc:
        log(f"[postgame] Stamm-Record {stale.name} nicht loeschbar ({exc!r}).")


def write_record(cfg, report: dict, *, log=print) -> Path | None:
    """Extrahiert den Trend-Record aus dem Report-Modell und schreibt ihn nach
    `postgame/trend/<id>.json` (idempotent - der neueste Lauf gewinnt).

    Ist die echte Match-ID inzwischen bekannt (Capture-Enrichment) und weicht sie
    vom urspruenglichen Stempel ab, wird der stale Stempel-Record entfernt, damit
    kein Doppel-Record desselben Spiels stehenbleibt. Zeigte der BISHERIGE Record
    desselben Spiels auf eine andere HTML-Datei, fliegt diese verdraengte Datei
    mit (s. `_drop_displaced_stamp`) - sonst bleibt sie als record-lose
    Karteileiche in der History stehen. Jeder Fehler wird geloggt und geschluckt -
    das Schreiben des Records darf den Report-Bau NIE brechen."""
    try:
        record = extract_record(report)
        if not record.get("match_id"):
            return None
        stamp = record.get("source_match_id")
        try:
            _drop_displaced_stamp(cfg, record["match_id"], stamp, log=log)
        except Exception as exc:   # noqa: BLE001 - Aufraeumen ist Kuer
            log(f"[postgame] Verdraengte Report-Datei nicht aufraeumbar ({exc!r}).")
        target = trend_dir(cfg) / f"{record['match_id']}.json"
        write_json(target, record)
        # Stale Stempel-Record aufraeumen (Capture: live_<...> -> echte ID).
        if stamp and stamp != record["match_id"]:
            stale = trend_dir(cfg) / f"{stamp}.json"
            if stale.exists():
                stale.unlink()
        return target
    except Exception as exc:   # noqa: BLE001 - Robustheit wie andere Anreicherungen
        log(f"[postgame] Trend-Record konnte nicht geschrieben werden ({exc!r}).")
        return None


def load_records(cfg, ident: str | None, *, log=print) -> tuple[list, bool]:
    """Alle Trend-Records laden und (optional) auf die Identitaet filtern.

    `ident` = Riot-ID 'Name#Tag' oder PUUID; ohne '#' und lang genug gilt es als
    PUUID. Ohne Identitaet werden ALLE Records genommen (mit Warnhinweis - dann
    koennen fremde Spiele mit drin sein). Rueckgabe (records, gefiltert): die nach
    Datum absteigend sortierten Records und ob nach Identitaet gefiltert wurde."""
    d = trend_dir(cfg)
    records = []
    if d.exists():
        for f in d.glob("*.json"):
            rec = read_json(f)
            if isinstance(rec, dict) and rec.get("match_id"):
                records.append(rec)
    ident = (ident or "").strip()
    filtered = False
    if ident:
        name_key = ident.split("#", 1)[0].strip().lower()
        is_puuid = "#" not in ident and len(ident) >= 20
        sel = []
        for rec in records:
            rec_name = (rec.get("name") or "").split("#", 1)[0].strip().lower()
            if rec_name and rec_name == name_key:
                sel.append(rec)
            elif is_puuid and rec.get("puuid") == ident:
                sel.append(rec)
        records = sel
        filtered = True
    else:
        log("[postgame] Keine Identitaet (me:/--me) - Trend ueber ALLE Records "
            "(kann fremde Spiele enthalten).")
    records.sort(key=lambda r: r.get("date_ms", 0), reverse=True)
    return records, filtered


# ============================================================================
# 3. Aggregation -> Prioliste + Splits
# ============================================================================

def _severity_delta(mean_delta: float, metric: str) -> float:
    """Normierte Schwere (0..1) eines mittleren Delta-Rueckstands.

    Nur ein Rueckstand (mean_delta < 0) hat Schwere; ein Vorsprung ist kein
    Uebungsthema -> 0. severity = min(1, |mean_delta| / DELTA_SCALE[metric])."""
    if mean_delta >= 0:
        return 0.0
    scale = DELTA_SCALE.get(metric, 1.0)
    return min(1.0, abs(mean_delta) / scale)


def _delta_finding(records: list, metric: str) -> dict | None:
    """Prioliste-Eintrag fuer eine Delta-Metrik (gold/cs/vision/dmg).

    Auswertbar sind nur Spiele mit vorhandenem Delta (Gegenpart + ggf. Schaden).
    Rueckgabe None, wenn < MIN_TREND_GAMES auswertbare Spiele ODER der Spieler im
    Mittel NICHT hinten ist (kein Uebungsthema)."""
    per_game = []          # chronologisch (aeltestes zuerst) fuer die Mini-Balken
    for rec in reversed(records):
        d = (rec.get("deltas") or {}).get(metric)
        if d is None:
            continue
        per_game.append({"value": d, "win": rec.get("win"),
                         "champ": rec.get("champ")})
    n = len(per_game)
    if n < MIN_TREND_GAMES:
        return None
    vals = [g["value"] for g in per_game]
    mean_delta = sum(vals) / n
    behind = sum(1 for v in vals if v < 0)
    severity = _severity_delta(mean_delta, metric)
    consistency = behind / n
    score = consistency * severity
    if score <= 0:
        return None
    return {"key": metric, "kind": "delta", "label": METRIC_LABEL[metric],
            "mean": mean_delta, "behind": behind, "n": n,
            "consistency": consistency, "severity": severity, "score": score,
            "per_game": per_game}


def _quote_finding(records: list) -> dict | None:
    """Prioliste-Eintrag fuer die Impact-Quote (nur has_damage-Spiele mit Quote).

    Schwere = wie weit die mittlere Quote unter 1.0 liegt (min(1, 1-mean_quote)).
    None bei < MIN_TREND_GAMES auswertbaren Spielen oder mittlerer Quote >= 1."""
    per_game = []
    for rec in reversed(records):
        q = rec.get("impact_quote")
        if q is None:
            continue
        per_game.append({"value": q, "win": rec.get("win"),
                         "champ": rec.get("champ")})
    n = len(per_game)
    if n < MIN_TREND_GAMES:
        return None
    vals = [g["value"] for g in per_game]
    mean_q = sum(vals) / n
    behind = sum(1 for v in vals if v < QUOTE_BEHIND)
    severity = min(1.0, max(0.0, QUOTE_BEHIND - mean_q))
    consistency = behind / n
    score = consistency * severity
    if score <= 0:
        return None
    return {"key": "impact_quote", "kind": "quote", "label": QUOTE_LABEL,
            "mean": mean_q, "behind": behind, "n": n,
            "consistency": consistency, "severity": severity, "score": score,
            "per_game": per_game}


def _early_death_finding(records: list) -> dict | None:
    """Prioliste-Eintrag fuer fruehe Tode (>= EARLY_DEATH_MIN vor Min 10).

    Auswertbar in JEDEM Spiel (keine Key-Bindung). Schwere = min(1, mean_early /
    EARLY_DEATH_MIN). None, wenn der Befund in keinem Spiel auftrat."""
    per_game = []
    for rec in reversed(records):
        early = int((rec.get("deaths") or {}).get("early", 0) or 0)
        per_game.append({"value": early, "win": rec.get("win"),
                         "champ": rec.get("champ")})
    n = len(per_game)
    if n < MIN_TREND_GAMES:
        return None
    vals = [g["value"] for g in per_game]
    mean_e = sum(vals) / n
    triggered = sum(1 for v in vals if v >= EARLY_DEATH_MIN)
    severity = min(1.0, mean_e / EARLY_DEATH_MIN) if EARLY_DEATH_MIN else 0.0
    consistency = triggered / n
    score = consistency * severity
    if score <= 0:
        return None
    return {"key": "early_deaths", "kind": "count", "label": EARLY_DEATH_LABEL,
            "mean": mean_e, "behind": triggered, "n": n,
            "consistency": consistency, "severity": severity, "score": score,
            "per_game": per_game}


def _split(records: list, key: str) -> list:
    """Split-Tabellen je Rolle bzw. Champion: Winrate + Kern-Deltas (Mittel).

    `key` = 'role' oder 'champ'. Rueckgabe je Gruppe {name, games, wins, losses,
    winrate|None, deltas:{gold,cs,vision}} - nach Spielzahl absteigend."""
    groups: dict[str, list] = {}
    for rec in records:
        name = rec.get(key) or "?"
        groups.setdefault(name, []).append(rec)
    out = []
    for name, recs in groups.items():
        wins = sum(1 for r in recs if r.get("win") is True)
        losses = sum(1 for r in recs if r.get("win") is False)
        decided = wins + losses
        deltas = {}
        for m in ("gold", "cs", "vision"):
            vals = [(r.get("deltas") or {}).get(m) for r in recs]
            vals = [v for v in vals if v is not None]
            deltas[m] = round(sum(vals) / len(vals), 1) if vals else None
        out.append({
            "name": name, "games": len(recs), "wins": wins, "losses": losses,
            "winrate": (wins / decided) if decided else None, "deltas": deltas,
        })
    out.sort(key=lambda g: g["games"], reverse=True)
    return out


def aggregate(records: list, n: int = DEFAULT_N) -> dict:
    """Aggregiert die N neuesten Records zur Prioliste + Splits + Meta.

    `records` muss nach Datum absteigend vorsortiert sein (load_records). Es
    werden die ersten `n` genommen. Prioliste = alle auswertbaren Befunde (>=
    MIN_TREND_GAMES Spiele), sortiert nach Konsistenz x Schwere (score) absteigend.
    Splits je Rolle/Champion und wiederkehrende Befunde als Text. Meta traegt N,
    Zeitraum und wie viele Records key-frei (ohne Schaden) waren."""
    window = list(records[:n]) if n and n > 0 else list(records)
    n_games = len(window)

    findings = []
    for metric in ("gold", "cs", "vision", "dmg"):
        f = _delta_finding(window, metric)
        if f:
            findings.append(f)
    q = _quote_finding(window)
    if q:
        findings.append(q)
    ed = _early_death_finding(window)
    if ed:
        findings.append(ed)
    findings.sort(key=lambda f: f["score"], reverse=True)

    # Wiederkehrende Befunde als Text (aus den Findings, konsistenteste zuerst).
    recurring = [f"{f['label']} in {f['behind']}/{f['n']} Spielen"
                 for f in sorted(findings, key=lambda f: f["consistency"],
                                 reverse=True)]

    dates = [r.get("date_ms") for r in window if r.get("date_ms")]
    keyless = sum(1 for r in window if not r.get("has_damage"))

    return {
        "n": n_games,
        "priority": findings,
        "by_role": _split(window, "role"),
        "by_champ": _split(window, "champ"),
        "recurring": recurring,
        "meta": {
            "n": n_games,
            "date_from": min(dates) if dates else None,
            "date_to": max(dates) if dates else None,
            "keyless": keyless,
            "with_damage": n_games - keyless,
        },
    }


# ============================================================================
# 4. Verdikt-Anbindung (Einzel-Report)
# ============================================================================

def top_finding(agg: dict) -> dict | None:
    """Top-Eintrag der Prioliste (hoechster Konsistenz-x-Schwere-Score) oder None."""
    prio = (agg or {}).get("priority") or []
    return prio[0] if prio else None


def occurs_in_record(finding: dict, record: dict) -> bool:
    """Trat der aggregierte Befund auch im aktuellen Einzel-Spiel-Record auf?

    delta-Metrik: eigener Gesamt-Delta < 0 (hinten). quote: Quote < QUOTE_BEHIND.
    count (early_deaths): >= EARLY_DEATH_MIN fruehe Tode. Fehlt der Wert im Record
    -> False (kein irrefuehrendes Anhaengen)."""
    if not finding or not record:
        return False
    kind = finding.get("kind")
    if kind == "delta":
        d = (record.get("deltas") or {}).get(finding["key"])
        return d is not None and d < 0
    if kind == "quote":
        q = record.get("impact_quote")
        return q is not None and q < QUOTE_BEHIND
    if kind == "count":
        early = int((record.get("deaths") or {}).get("early", 0) or 0)
        return early >= EARLY_DEATH_MIN
    return False


def verdict_trend_line(cfg, report: dict, *, n: int = DEFAULT_N,
                       min_games: int = MIN_TREND_GAMES, log=print) -> str | None:
    """Eine Trend-Zeile fuers Einzel-Verdikt oder None (graceful).

    Bedingungen (§6 Phase 6): Es existieren >= `min_games` Records DERSELBEN
    Identitaet (die bereits gebauten frueheren Spiele - das aktuelle ist beim
    Report-Bau noch nicht geschrieben) UND der Top-Priolisten-Befund dieses
    Trends trat auch im aktuellen Spiel auf. Jeder Fehler -> None; die Zeile darf
    den Report-Bau nie brechen."""
    try:
        ident = (report.get("me") or {}).get("name") or cfg.me
        records, _filtered = load_records(cfg, ident, log=log)
        # Das aktuelle Spiel kann bereits als (aelterer) Record vorliegen (Re-Run
        # desselben Matches) - dann nicht doppelt gegen sich selbst zaehlen.
        current = extract_record(report)
        records = [r for r in records if r.get("match_id") != current["match_id"]]
        if len(records) < min_games:
            return None
        agg = aggregate(records, n)
        top = top_finding(agg)
        if not top or not occurs_in_record(top, current):
            return None
        return (f"{top['label']} — zieht sich durch deine letzten "
                f"{agg['n']} Spiele ({top['behind']}/{top['n']}).")
    except Exception as exc:   # noqa: BLE001 - nie den Report brechen
        log(f"[postgame] Trend-Verdikt-Zeile uebersprungen ({exc!r}).")
        return None


# ============================================================================
# 5. CLI-Laeufe: Trend-Report bauen + Backfill
# ============================================================================

def run_trend(cfg, *, n: int = DEFAULT_N, me: str | None = None,
              out_path: Path | None = None, log=print) -> Path:
    """Baut `postgame/trend.html` aus den vorhandenen Records der Identitaet.

    Laedt die Records (Identitaets-Filter `me:`/--me), aggregiert die N neuesten
    (Prioliste + Splits) und rendert die self-contained HTML-Seite. Gibt den
    Pfad zurueck."""
    from . import render
    ident = (me or cfg.me or "").strip() or None
    records, filtered = load_records(cfg, ident, log=log)
    if not records:
        log("[postgame] Keine Trend-Records gefunden - erst Reports bauen "
            "(postgame <matchId>) oder --trend-backfill nutzen.")
    agg = aggregate(records, n)
    html = render.render_trend_html(agg, ident=ident if filtered else None)
    target = Path(out_path) if out_path else (cfg.postgame_out_dir / "trend.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    log(f"[postgame] Trend ueber {agg['n']} Spiele: {len(agg['priority'])} "
        f"Befund(e) in der Prioliste.")
    return target


def _existing_record_ids(cfg) -> set:
    """Set der bereits als Record vorhandenen Match-IDs (Dateinamen ohne .json)."""
    d = trend_dir(cfg)
    if not d.exists():
        return set()
    return {f.stem for f in d.glob("*.json")}


def backfill(cfg, *, n: int = DEFAULT_N, me: str | None = None,
             log=print) -> int:
    """Erzeugt fehlende Trend-Records fuer die letzten N SR-5v5-Spiele der
    Identitaet (Key noetig - der Nutzer startet das selbst).

    Nutzt die bestehende --latest-Aufloesung (ungefilterte match_ids + SR-Filter,
    s. fetch): Identitaet -> PUUID -> die letzten `lookback` Match-IDs -> je Spiel
    pruefen, ob es SR-5v5 ist und noch kein Record existiert; wenn ja, cache-first
    Match+Timeline -> build_report -> Record. Fehler je Spiel werden geloggt und
    UEBERSPRUNGEN (kein Abbruch). Rueckgabe = Anzahl neu erzeugter Records."""
    from . import build_report, fetch
    ident = (me or cfg.me or "").strip()
    if not ident:
        raise SystemExit(
            "[postgame] --trend-backfill braucht eine Identitaet: --me "
            "'Name#Tag', --puuid <PUUID> oder 'me:' in config.yml setzen.")

    client = fetch._region_client(cfg)
    puuid = fetch._resolve_puuid(client, ident, log=log)
    if not puuid:
        raise SystemExit(
            f"[postgame] Identitaet '{ident}' nicht aufloesbar - Riot-ID pruefen "
            f"(Name#Tag) oder direkt --puuid angeben.")

    ids = client.match_ids(puuid, queue=None, count=max(n * 2, n),
                           type_filter=None) or []
    if not ids:
        log(f"[postgame] Keine Matches fuer '{ident}' gefunden.")
        return 0

    existing = _existing_record_ids(cfg)
    made = 0
    for mid in ids:
        if made >= n:
            break
        if mid in existing:
            continue
        qid = fetch._queue_id_of(cfg, client, mid, log=log)
        if qid not in fetch.SR_5V5_QUEUES:
            continue
        try:
            log(f"[postgame] Backfill {mid} ...")
            report = build_report(cfg, mid, me=ident, log=log)
            write_record(cfg, report, log=log)
            made += 1
        except Exception as exc:   # noqa: BLE001 - je Spiel ueberspringen
            log(f"[postgame] Backfill {mid} uebersprungen ({exc!r}).")
    log(f"[postgame] Backfill fertig: {made} neue Record(s).")
    return made
