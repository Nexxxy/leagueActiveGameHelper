"""Match-History der Post-Game-Reports (Nutzer-Wunsch 2026-07-29).

Zwei Bausteine, beide bewusst OHNE FastAPI-Bezug (der Server in `server.py`
verdrahtet sie nur, die Logik bleibt ohne HTTP testbar):

  1. **Liste der letzten Spiele** (`list_games`): scannt den postgame-Ordner,
     joint jede HTML-Datei mit ihrem Trend-Record (`postgame/trend/<id>.json`)
     und liefert je Spiel Datum, Champion/Rolle, Ergebnis und die
     **Daten-Qualitaet** (ok/pending/failed/no_key/disabled/unknown) plus die
     Win/Loss-Quote ueber die Spiele mit bekanntem Ausgang.

  2. **Retry** (`start_retry`): baut einen unvollstaendigen Report ueber die
     Riot-API neu. Er sucht das Spiel in der Match-History des Nutzers
     (Roster-Match, sonst Zeitstempel+Champion), baut den VOLLEN Timeline-Report
     und ueberschreibt DIESELBE HTML-Datei - die verlinkte URL bleibt also
     stabil. Er laeuft asynchron; ein Registry-Dict traegt den Zustand
     ("running"/"done"/"failed:<grund>") fuer die naechsten `list_games`-Aufrufe.

  3. **Sammel-Retry** (`start_retry_all`): dieselbe Arbeit fuer ALLE aktuell
     unvollstaendigen Reports, sequenziell in einem Thread (Rate-Limit).

  4. **Manueller Load** (`start_load`): ein Spiel ueber Platform + GameID aus
     dem Riot-Client nachladen (Match-ID = `<PLATFORM>_<gameid>`) - ohne
     Capture, ohne Resolver, ohne Identitaet.

Deckt ein Report dasselbe Spiel ab wie ein anderer (Doppel-Capture des
Watchers), fuehren Retry/Load die beiden zusammen: es bleibt genau EINE Datei je
Spiel (s. `_rebuild_report`, `_mark_duplicates`).

Robustheit vor Vollstaendigkeit: jeder IO-/Netz-Pfad ist gekapselt, ein Fehler
wird geloggt und degradiert die Zeile (Status "unknown"), aber er darf den
Server nie crashen.
"""

import re
import threading
import time
from html import unescape
from pathlib import Path

from core.cacheio import read_json
from .postgame import enrich, fetch, trend

# Wie viele Spiele die History maximal zeigt (Nutzer-Wunsch: die letzten 20).
DEFAULT_LIMIT = 20

# Dateien im postgame-Ordner, die KEIN Einzel-Report sind.
_NOT_A_REPORT = frozenset({"trend.html"})

# Status, die einen Retry rechtfertigen (der Report ist unvollstaendig und es
# laeuft nichts mehr, was ihn von selbst vervollstaendigt).
_RETRYABLE = frozenset({"failed", "no_key", "unknown"})

# Ein Datei-Stamm, der schon eine ECHTE Match-ID ist (z.B. "EUW1_7932947396")
# im Gegensatz zum Capture-Stempel eines Live-Reports ("live_20260728_221355").
_REAL_MATCH_ID = re.compile(r"^[A-Z0-9]+_\d+$")

# Ab wann ein "pending"-Report als haengengeblieben gilt (der Watcher schreibt
# bei jedem Stufe-2-Versuch die Datei neu - eine alte mtime heisst also, dass
# niemand mehr daran arbeitet, z.B. nach einem Server-Neustart).
PENDING_STALE_SECONDS = 30 * 60

# Wie viele Match-IDs der Retry-Resolver durchsieht, wenn KEIN Datum bekannt
# ist (Notpfad: die letzten N Spiele ueber alle Queues).
RETRY_LOOKBACK = 30

# Zeitfenster fuer den Zeitstempel-Fallback des Resolvers (Alt-Records ohne
# Roster): das Match muss innerhalb dieser Spanne um das Record-Datum enden.
# 60 min statt der urspruenglichen 30 (Bugfix 2026-07-30): das Record-Datum ist
# der Zeitpunkt, an dem der Watcher das Capture FINALISIERT hat - real gemessene
# Deltas von 34,8/34,9 min fielen aus dem alten Fenster. Der Best-Delta-
# Mechanismus nimmt weiterhin den zeitlich naechsten Kandidaten (und der
# Roster-Weg hat ohnehin Vorrang), das groessere Fenster ist also nur ein
# Sicherheitsnetz, kein Praezisionsverlust.
RETRY_TIME_TOLERANCE_MS = 60 * 60 * 1000

# Zeitabstand, bis zu dem zwei Reports mit IDENTISCHEM (kanonisiertem) Roster
# als dasselbe Spiel gelten (Duplikat-Erkennung ohne Retry, s. `_mark_duplicates`).
DUP_TIME_TOLERANCE_MS = 6 * 60 * 60 * 1000

# Zeitfenster, mit dem der Resolver die Match-IDs bei der API anfragt (Sekunden,
# relativ zum Report-Datum). Grosszuegig nach hinten (6 h), weil das
# Report-Datum die ERSTELLUNG des Reports ist und eine lange Session/ein spaeter
# Retry beliebig weit dahinter liegen kann; nach vorn reicht 1 h Unschaerfe.
# Effekt: statt "die letzten 30 Spiele" (die bei ARAM-Abenden Tage alte Reports
# nie erreichen) fragt Riot nativ nur die Spiele dieses Abends ab - findet also
# auch alte Reports und kostet nur EINEN ID-Request mit wenigen Kandidaten.
RETRY_WINDOW_BEFORE_S = 6 * 60 * 60
RETRY_WINDOW_AFTER_S = 60 * 60

# Wie viele IDs das Zeitfenster maximal liefern darf (API-Maximum 100; das
# Fenster selbst begrenzt die Menge, der Wert ist nur die Obergrenze).
RETRY_WINDOW_COUNT = 100

# Pause zwischen zwei Spielen im Sammel-Retry (Rate-Limit-freundlich).
RETRY_BATCH_PAUSE_S = 1.5


# ============================================================================
# 1. Status-Ermittlung (Record-Feld, sonst HTML-Sniffing)
# ============================================================================

# Marker der vier Disclaimer-Texte aus `render._DISCLAIMER_BODY`. Nur fuer
# ALTE Reports noetig, deren Trend-Record noch kein `damage_status` traegt -
# darum bewusst reine String-Suche statt HTML-Parser.
_DISCLAIMER_MARKER = 'class="disclaimer"'
_STATUS_MARKERS = (("wird nachgeladen", "pending"),
                   ("nicht möglich", "failed"),
                   ("Kein API-Key", "no_key"),
                   ("übersprungen", "disabled"))

# (Pfad -> (mtime, status)): das Sniffing liest die ganze Datei, darum wird das
# Ergebnis je Datei-Stand gemerkt. Ein Neuschreiben aendert die mtime und
# invalidiert den Eintrag automatisch.
_SNIFF_CACHE: dict[str, tuple[float, str]] = {}
_SNIFF_LOCK = threading.Lock()


def _sniff_status(path: Path) -> str:
    """Daten-Qualitaet eines Reports aus dem HTML ableiten (Fallback).

    Ohne Disclaimer-Block ist der Report vollstaendig ("ok"); sonst entscheidet
    der Disclaimer-Text. Unlesbare Datei -> "unknown"."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "unknown"
    key = str(path)
    with _SNIFF_LOCK:
        hit = _SNIFF_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "unknown"
    if _DISCLAIMER_MARKER not in html:
        status = "ok"
    else:
        status = "unknown"
        for marker, value in _STATUS_MARKERS:
            if marker in html:
                status = value
                break
    with _SNIFF_LOCK:
        _SNIFF_CACHE[key] = (mtime, status)
    return status


# --- Roster/Champion aus dem HTML (Resolver-Fallback ohne Record) ------------
# WARUM (Bugfix 2026-07-30, realer Vorfall): Verliert eine Report-Datei ihren
# Trend-Record (ein anderer Lauf hat den Record desselben Spiels auf einen
# anderen Stempel umgehaengt), hatte der Retry KEINE Suchmerkmale mehr -
# `_rebuild_report` zieht Roster und Champion aus `rec`, und ohne die liefert
# `resolve_match_id` nichts. Die Zeile blieb als "unbekannt" liegen und war
# nicht heilbar. Die gerenderte HTML traegt beide Merkmale aber selbst: die
# Roster-Sektion listet alle 10 Champions, und die eigene Team-Karte ist mit
# dem DU-Badge markiert. Reine String-/Regex-Suche wie beim Status-Sniffing -
# ein HTML-Parser waere fuer zwei feste Markup-Stellen unverhaeltnismaessig.
_RO_CHAMP = re.compile(r'<span class="ro-champ">([^<]*)</span>')
_ME_CHAMP = re.compile(r'<div class="tc-champ">([^<]*)'
                       r'<span class="me-badge">')

# Platzhalter, die `render._roster_row` fuer eine fehlende Seite schreibt.
_RO_PLACEHOLDER = frozenset({"", "—"})

# (Pfad -> (mtime, (roster, champ))): dasselbe mtime-Cache-Muster wie beim
# Status-Sniffing - der Retry-Pfad liest die Datei sonst je Versuch neu.
_ROSTER_CACHE: dict[str, tuple[float, tuple]] = {}


def _sniff_roster(path: Path) -> tuple[list, str | None]:
    """(Roster, eigener Champion) aus einer gerenderten Report-HTML.

    Roster = die Champions der Roster-Sektion (bis zu 10, Platzhalter fuer
    fehlende Gegenparts fliegen raus); Champion = der Champ der eigenen
    Team-Karte (DU-Badge). Unlesbare/fremde HTML -> ([], None)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [], None
    key = str(path)
    with _SNIFF_LOCK:
        hit = _ROSTER_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return list(hit[1][0]), hit[1][1]
    try:
        html_text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return [], None
    roster = [unescape(m).strip() for m in _RO_CHAMP.findall(html_text)]
    roster = [c for c in roster if c not in _RO_PLACEHOLDER]
    me = _ME_CHAMP.search(html_text)
    champ = unescape(me.group(1)).strip() if me else None
    with _SNIFF_LOCK:
        _ROSTER_CACHE[key] = (mtime, (list(roster), champ))
    return roster, champ


# ============================================================================
# 2. Retry-Registry (Zustand der laufenden/abgeschlossenen Retries)
# ============================================================================

_RETRY_STATE: dict[str, str] = {}
_RETRY_LOCK = threading.Lock()

# Laeuft gerade ein Sammel-Retry ("Alle nachladen")? Eigenes Flag mit eigenem
# Lock, damit ein zweiter Batch-Trigger abgelehnt wird, waehrend der erste noch
# seine Spiele abarbeitet (die Einzel-Spiele schuetzt _RETRY_STATE).
_BATCH_RUNNING = False
_BATCH_LOCK = threading.Lock()


def _set_retry_state(match_id: str, state: str) -> None:
    with _RETRY_LOCK:
        _RETRY_STATE[match_id] = state


def retry_state(match_id: str) -> str | None:
    """Zustand eines Retries: "running" | "done" | "failed:<grund>" | None."""
    with _RETRY_LOCK:
        return _RETRY_STATE.get(match_id)


def _set_batch_running(value: bool) -> None:
    global _BATCH_RUNNING
    with _BATCH_LOCK:
        _BATCH_RUNNING = value


def batch_running() -> bool:
    """Laeuft gerade ein Sammel-Retry?"""
    with _BATCH_LOCK:
        return _BATCH_RUNNING


def reset_retry_state() -> None:
    """Registry leeren (nur fuer Tests / Server-Neustart-Semantik)."""
    with _RETRY_LOCK:
        _RETRY_STATE.clear()
    _set_batch_running(False)


# ============================================================================
# 3. Liste der letzten Spiele
# ============================================================================

def _record_index(cfg) -> dict:
    """Trend-Records unter ihrer Match-ID UND ihrem Datei-Stempel indizieren.

    Ein Record kann unter der echten Match-ID liegen (`EUW1_...`), waehrend die
    HTML-Datei noch den Capture-Stempel traegt (`live_...`) - deshalb sind beide
    Schluessel noetig. Die echte ID gewinnt (erster Durchgang), der Stempel
    fuellt nur Luecken, damit ein stale Record keinen echten verdraengt."""
    out: dict[str, dict] = {}
    d = trend.trend_dir(cfg)
    if not d.exists():
        return out
    records = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = read_json(f)
        except Exception:   # noqa: BLE001 - eine kaputte Datei killt nicht alle
            continue
        if isinstance(rec, dict) and rec.get("match_id"):
            records.append(rec)
    for rec in records:
        out[rec["match_id"]] = rec
    for rec in records:
        stamp = rec.get("source_match_id")
        if stamp:
            out.setdefault(stamp, rec)
    return out


def _report_files(cfg) -> list:
    """Alle Einzel-Report-HTMLs im postgame-Ordner (trend.html ausgenommen).

    Nicht-HTML (Archive, Screenshots) faellt schon durch das Glob-Muster."""
    out_dir = cfg.postgame_out_dir
    if not out_dir.exists():
        return []
    return [p for p in sorted(out_dir.glob("*.html"))
            if p.name not in _NOT_A_REPORT and p.is_file()]


# ----------------------------------------------------------------------------
# 3b. Duplikate: zwei Dateien, EIN Spiel
# ----------------------------------------------------------------------------
# WARUM (Bugfix 2026-07-30): Der Watcher kann dasselbe Spiel zweimal
# mitschneiden (z.B. nach einem Client-Reconnect) - dann liegen zwei Reports
# desselben Matches mit unterschiedlicher Datenqualitaet in der Liste. Die
# History zeigt dann zwei Zeilen fuer EIN Spiel und verfaelscht die Win/Loss-
# Quote. Erkennung ueber die echte Match-ID des Records bzw. - wenn die noch
# nicht bekannt ist - ueber das identische 10er-Roster in einem engen Zeitfenster.

# Rang der Daten-Qualitaet: welcher von zwei Reports desselben Spiels bleibt?
_QUALITY_RANK = {"ok": 2, "pending": 1}


def _report_status(path: Path, index: dict | None = None) -> str:
    """Daten-Qualitaet eines Reports (Record-Feld, sonst HTML-Sniffing)."""
    rec = (index or {}).get(path.stem) or {}
    return rec.get("damage_status") or _sniff_status(path)


def _duplicate_files(cfg, real_id: str, path: Path | None = None,
                     index: dict | None = None) -> list:
    """Andere Report-Dateien, die dasselbe Spiel (`real_id`) abdecken.

    Zwei Wege, beide ohne Netz: die Datei `<real_id>.html` (voller Report aus
    `pipeline postgame`) und jede Datei, deren Trend-Record bereits auf diese
    Match-ID zeigt (`match_id == real_id`, Datei unter `source_match_id`). Die
    Datei unter der echten Match-ID steht immer vorn - sie ist bei sonst
    gleicher Qualitaet der bessere Zielort. `path` (die Datei, um die es gerade
    geht) ist nie im Ergebnis."""
    out: list[Path] = []
    real_file = cfg.postgame_out_dir / f"{real_id}.html"
    if real_file != path and real_file.exists():
        out.append(real_file)
    if index is None:
        try:
            index = _record_index(cfg)
        except Exception:   # noqa: BLE001 - Records sind optional
            index = {}
    for rec in index.values():
        if rec.get("match_id") != real_id:
            continue
        stamp = rec.get("source_match_id")
        if not stamp:
            continue
        cand = cfg.postgame_out_dir / f"{stamp}.html"
        if cand != path and cand not in out and cand.exists():
            out.append(cand)
    return out


def _existing_report(cfg, real_id: str, index: dict | None = None):
    """Der bereits vorhandene Report zu `real_id` (oder None).

    Bevorzugt die Datei unter der echten Match-ID; sonst die Live-Datei, deren
    Record schon auf dieses Match zeigt."""
    files = _duplicate_files(cfg, real_id, None, index)
    return files[0] if files else None


def _drop_report(cfg, path: Path, real_id: str, *, log=print) -> None:
    """Eine Duplikat-Datei samt ihres verwaisten Stamm-Records loeschen.

    Der Record wird NUR entfernt, wenn er noch der Stempel-Record dieser Datei
    ist (`match_id == <stamp>`) - der Record des BEHALTENEN Reports laeuft unter
    der echten Match-ID und darf nie mitgeloescht werden."""
    try:
        path.unlink()
    except OSError as exc:
        log(f"[history] Duplikat {path.name} nicht loeschbar ({exc!r}).")
        return
    log(f"[history] Duplikat {path.name} entfernt (Spiel {real_id} bleibt).")
    if path.stem == real_id:
        return
    rec_path = trend.trend_dir(cfg) / f"{path.stem}.json"
    try:
        if not rec_path.exists():
            return
        rec = read_json(rec_path)
        if isinstance(rec, dict) and rec.get("match_id") == path.stem:
            rec_path.unlink()
    except Exception as exc:   # noqa: BLE001 - Aufraeumen ist Kuer
        log(f"[history] Stamm-Record {rec_path.name} nicht loeschbar ({exc!r}).")


def _duplicate_clusters(pairs: list) -> list:
    """Gruppen von (Zeile, Record)-Paaren, die dasselbe Spiel meinen.

    Zwei Kriterien: (a) identische echte Match-ID im Record, (b) identisches
    kanonisiertes 10er-Roster mit weniger als DUP_TIME_TOLERANCE_MS Abstand
    (dasselbe Roster in einem Fenster von Stunden ist praktisch nur dasselbe
    Spiel - Champions sind pro SR-Spiel eindeutig)."""
    out: list[list] = []
    by_id: dict[str, list] = {}
    for pair in pairs:
        mid = (pair[1] or {}).get("match_id")
        if mid:
            by_id.setdefault(mid, []).append(pair)
    out.extend(g for g in by_id.values() if len(g) > 1)

    by_roster: dict[frozenset, list] = {}
    for pair in pairs:
        roster = (pair[1] or {}).get("roster") or []
        if len(roster) >= 10:
            by_roster.setdefault(enrich._canon_champs(roster), []).append(pair)
    for group in by_roster.values():
        if len(group) < 2:
            continue
        cluster: list = []
        for pair in sorted(group, key=lambda p: p[0]["date_ms"]):
            if cluster and (pair[0]["date_ms"] - cluster[-1][0]["date_ms"]
                            > DUP_TIME_TOLERANCE_MS):
                if len(cluster) > 1:
                    out.append(cluster)
                cluster = []
            cluster.append(pair)
        if len(cluster) > 1:
            out.append(cluster)
    return out


def _sniffed_pairs(cfg, pairs: list) -> list:
    """Fuer die Duplikat-Erkennung: record-lose Zeilen mit dem Roster aus ihrer
    HTML anreichern.

    Nur Zeilen OHNE Trend-Record werden angefasst (die haben ohnehin schon einen
    HTML-Lesevorgang fuers Status-Sniffing hinter sich und sind der seltene
    Sonderfall) - fuer jede Zeile HTML zu parsen waere zu teuer. Damit landet
    genau die record-lose Karteileiche im Roster-Cluster ihres vollstaendigen
    Zwillings, wird retrybar und heilt sich ueber die Merge-Logik selbst. Die
    Zeilen-Dicts sind dieselben Objekte wie in `pairs` - `_mark_duplicates`
    markiert also weiterhin in place."""
    out = []
    for row, rec in pairs:
        if rec:
            out.append((row, rec))
            continue
        path = cfg.postgame_out_dir / f"{row['match_id']}.html"
        roster, champ = _sniff_roster(path)
        out.append((row, {"roster": roster, "champ": champ} if roster else rec))
    return out


def _mark_duplicates(pairs: list, has_key: bool) -> None:
    """Duplikat-Zeilen markieren: die schlechtere wird retrybar (in place).

    Gewinner je Gruppe ist der Report mit der besseren Daten-Qualitaet (ok >
    pending > Rest), bei Gleichstand die Datei unter der echten Match-ID. Alle
    anderen bekommen `duplicate_of` gesetzt und werden retrybar - der Retry
    laeuft dann in die Merge-Logik (`_rebuild_report`) und raeumt die Zeile weg."""
    for cluster in _duplicate_clusters(pairs):
        best = max(cluster, key=lambda p: (
            _QUALITY_RANK.get(p[0]["status"], 0),
            bool(_REAL_MATCH_ID.match(p[0]["match_id"]))))
        for row, _rec in cluster:
            if row is best[0]:
                continue
            row["duplicate_of"] = best[0]["match_id"]
            if has_key and not row["retry_running"]:
                row["retryable"] = True


def _row(path: Path, rec: dict | None, now_s: float, has_key: bool) -> dict:
    """Eine History-Zeile aus Datei + (optionalem) Trend-Record bauen."""
    rec = rec or {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = now_s
    date_ms = rec.get("date_ms") or int(mtime * 1000)
    # Bevorzugt das Record-Feld (billig + exakt), sonst das HTML sniffen.
    status = rec.get("damage_status") or _sniff_status(path)
    # Ein "pending"-Report, an dem seit PENDING_STALE_SECONDS niemand mehr
    # geschrieben hat, ist haengengeblieben (Server-Neustart) -> retrybar.
    stale_pending = (status == "pending"
                     and (now_s - mtime) > PENDING_STALE_SECONDS)
    # Daten-Qualitaet und Ausgang sind ZWEI Dimensionen: ein Report kann
    # vollstaendig angereichert sein ("ok") und der Trend-Record trotzdem keinen
    # Ausgang tragen. Altlast-Fall: das HTML wurde von Stufe 2 angereichert,
    # bevor es den Record-Sync nach Stufe 2 und die win-Extraktion aus den
    # Match-Daten ueberhaupt gab - der Record blieb auf win=null stehen. Ohne
    # diesen Zweig gaebe es fuer solche Zeilen KEINEN Retry-Button (Status "ok"
    # steht bewusst nicht in _RETRYABLE), der fehlende Ausgang waere also nicht
    # nachladbar.
    ok_without_outcome = (status == "ok" and rec.get("win") is None)
    state = retry_state(path.stem)
    return {
        "match_id": path.stem,
        "url": f"/reports/{path.name}",
        "date_ms": int(date_ms),
        "champ": rec.get("champ"),
        "role": rec.get("role"),
        "queue": rec.get("queue"),
        "win": rec.get("win"),
        "status": status,
        "retryable": bool(has_key and (status in _RETRYABLE or stale_pending
                                       or ok_without_outcome)),
        "retry_running": state == "running",
        "retry_error": (state.split(":", 1)[1] if state
                        and state.startswith("failed:") else None),
        # Wird von `_mark_duplicates` gesetzt, wenn eine ANDERE Zeile dasselbe
        # Spiel besser abdeckt (dann ist diese hier retrybar -> Merge).
        "duplicate_of": None,
    }


def list_games(cfg, *, limit: int = DEFAULT_LIMIT, now_s: float | None = None,
               log=print) -> dict:
    """Die letzten `limit` Spiele aus dem postgame-Ordner + Win/Loss-Quote.

    Rueckgabe::

        {"games": [ {match_id, url, date_ms, champ, role, win, status,
                     retryable, retry_running, retry_error}, ... ],
         "wins": int, "losses": int, "unknown": int, "winrate_pct": int|None}

    Sortiert nach Datum absteigend (Trend-Record `date_ms`, sonst Datei-mtime).
    Spiele mit unbekanntem Ausgang (`win` = null, z.B. key-freier Live-Report)
    zaehlen NICHT in die Quote - sie erscheinen nur als `unknown`.

    Ein Spiel erscheint genau einmal: baut der Retry (oder ein manueller
    `pipeline postgame <id>`-Lauf) einen vollen Report zum selben Spiel, zeigen
    beide Dateien auf denselben Record - dann gewinnt die Datei, die unter der
    echten Match-ID liegt. Zeigen zwei Zeilen auf dasselbe Spiel, ohne dass der
    Record das schon weiss (Doppel-Capture, s. `_mark_duplicates`), bleibt die
    bessere stehen und die schlechtere wird retrybar - der Retry fuehrt sie
    dann zusammen."""
    try:
        index = _record_index(cfg)
    except Exception as exc:   # noqa: BLE001 - Records sind optional
        log(f"[history] Trend-Records nicht lesbar ({exc!r}) - History ohne "
            f"Zusatzdaten.")
        index = {}
    now_s = time.time() if now_s is None else now_s
    has_key = bool(cfg.active_api_keys)

    by_game: dict[str, tuple] = {}
    for path in _report_files(cfg):
        rec = index.get(path.stem)
        try:
            row = _row(path, rec, now_s, has_key)
        except Exception as exc:   # noqa: BLE001 - eine kaputte Datei killt nicht alles
            log(f"[history] Report {path.name} uebersprungen ({exc!r}).")
            continue
        # Dedupe-Schluessel = echte Match-ID des Spiels (sonst der Dateiname).
        key = (rec or {}).get("match_id") or path.stem
        prev = by_game.get(key)
        if prev is None or path.stem == key:
            # Die Datei unter der echten Match-ID ist der vollstaendige Report.
            by_game[key] = (row, rec or {})
    # Zwei Zeilen, EIN Spiel (Doppel-Capture): die schlechtere wird retrybar,
    # damit der Retry sie ueber die Merge-Logik zusammenfuehrt und entfernt.
    pairs = list(by_game.values())
    try:
        _mark_duplicates(_sniffed_pairs(cfg, pairs), has_key)
    except Exception as exc:   # noqa: BLE001 - Erkennung ist Kuer, nie crashen
        log(f"[history] Duplikat-Erkennung uebersprungen ({exc!r}).")
    rows = sorted((row for row, _rec in pairs),
                  key=lambda r: r["date_ms"], reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]

    wins = sum(1 for r in rows if r["win"] is True)
    losses = sum(1 for r in rows if r["win"] is False)
    decided = wins + losses
    return {
        "games": rows,
        "wins": wins,
        "losses": losses,
        "unknown": len(rows) - decided,
        "winrate_pct": round(wins / decided * 100) if decided else None,
    }


# ============================================================================
# 4. Retry: Report ueber die Riot-API neu befuellen
# ============================================================================

def _region_client(cfg):
    """RiotClient der Heimatregion (eigener Seam, in Tests gemockt)."""
    return fetch._region_client(cfg)


def _match_end_ms(match: dict) -> int | None:
    """Ende-Zeitstempel (ms) eines Matches: `gameEndTimestamp`, sonst
    `gameCreation` + Dauer. None, wenn beides fehlt."""
    info = (match.get("info") or {})
    end = info.get("gameEndTimestamp")
    if end:
        return int(end)
    created = info.get("gameCreation") or 0
    dur = info.get("gameDuration") or 0
    return int(created + dur * 1000) if created else None


def _is_me(p: dict, puuid: str | None, ident: str) -> bool:
    """Ist dieser Participant der Nutzer? PUUID ODER Riot-ID (key-unabhaengig).

    WARUM zwei Wege: Riot verschluesselt PUUIDs **pro API-Key**. Dasselbe Spiel
    liefert unter `riot.api_key` und `riot.dev_api_key` also VERSCHIEDENE
    PUUIDs. Die Match-Rohdaten liegen aber key-unabhaengig im Cache
    (Shard-Store `data/pipeline/store/matches/<patch>/`, `enrich._load_match`
    laedt Cache-first) - ein mit Key A gecachtes Match trifft eine mit Key B ueber
    account-v1 aufgeloeste PUUID nie. Der Vergleich ueber die Riot-ID
    (`riotIdGameName` + `riotIdTagline`) ist dagegen key-unabhaengig und rettet
    genau diesen Fall.

    `ident` ist `cfg.me`: eine Riot-ID 'Name#Tag' ODER direkt eine PUUID (dann
    bleibt nur der PUUID-Vergleich, zusaetzlich gegen `ident` selbst)."""
    pu = p.get("puuid")
    if pu and (pu == puuid or (ident and pu == ident)):
        return True
    if "#" not in ident:
        return False
    name, _, tag = ident.partition("#")
    return (str(p.get("riotIdGameName", "")).strip().lower()
            == name.strip().lower()
            and str(p.get("riotIdTagline", "")).strip().lower()
            == tag.strip().lower())


def _played_champ(match: dict, puuid: str | None, champ: str | None,
                  ident: str = "") -> bool:
    """Hat der Nutzer in diesem Match `champ` gespielt?

    Der Nutzer wird ueber `_is_me` erkannt (PUUID oder Riot-ID) - der
    Namensweg ist noetig, weil gecachte Matches von einem ANDEREN API-Key
    stammen koennen als die aktuell aufgeloeste PUUID (s. `_is_me`).

    Der Champion-Vergleich laeuft ueber `enrich._canon_champ`: der Record kann
    den ANZEIGENAMEN aus dem Live-Capture tragen ('Tahm Kench'), das Match
    liefert die ddragon-ID ('TahmKench') - roh verglichen scheiterte der
    Fallback an genau diesen Champions.

    Ohne bekannten Champion im Record ist die Pruefung nicht moeglich -> False
    (lieber kein Treffer als der falsche Report)."""
    if not champ:
        return False
    want = enrich._canon_champ(champ)
    for p in (match.get("info") or {}).get("participants", []) or []:
        if _is_me(p, puuid, ident):
            return enrich._canon_champ(p.get("championName")) == want
    return False


def _window_ids(client, puuid: str, date_ms: int, log=print) -> list:
    """Match-IDs im Zeitfenster um `date_ms` (Riot filtert serverseitig).

    Ein einziger Request, der typischerweise nur die Handvoll Spiele dieses
    Abends liefert - im Gegensatz zum Lookback-Pfad unabhaengig davon, wie viele
    Spiele seither gelaufen sind."""
    center = int(date_ms) // 1000
    ids = client.match_ids(
        puuid, queue=None, count=RETRY_WINDOW_COUNT,
        start_time=center - RETRY_WINDOW_BEFORE_S,
        end_time=center + RETRY_WINDOW_AFTER_S,
        type_filter=None,
    ) or []
    log(f"[history] Zeitfenster um das Report-Datum: {len(ids)} Kandidaten.")
    return list(ids)


def resolve_match_id(cfg, *, roster=None, date_ms=None, champ=None,
                     lookback: int = RETRY_LOOKBACK, log=print) -> str | None:
    """Echte Match-ID zu einem (Live-)Report finden - ohne das Capture.

    Ablauf: Identitaet (`cfg.me`) -> PUUID -> Kandidaten-IDs -> je Kandidat das
    Match Cache-first laden und pruefen:

      1. **Roster-Match** (bevorzugt): identische 10er-Champion-Menge wie im
         Trend-Record - in einem SR-Spiel eindeutig (s. `enrich._roster_matches`).
      2. **Zeitstempel + Champion** (Fallback fuer Alt-Records ohne `roster`):
         das Match endet innerhalb von RETRY_TIME_TOLERANCE_MS um das
         Record-Datum UND der Nutzer hat darin genau `champ` gespielt; von
         mehreren Kandidaten gewinnt der zeitlich naechste. Wer "der Nutzer"
         im Match ist, entscheidet `_is_me` bewusst key-unabhaengig (PUUID
         ODER Riot-ID) - gecachte Matches koennen von einem anderen API-Key
         stammen als die hier aufgeloeste PUUID.

    Die Kandidaten kommen bei bekanntem `date_ms` (Regelfall) aus dem
    ZEITFENSTER um dieses Datum (s. `_window_ids`) - das findet auch Reports von
    vor mehreren Tagen und laedt nur wenige Matches. Nur wenn das Fenster leer
    bleibt (oder gar kein Datum bekannt ist), greift der alte Pfad "die letzten
    `lookback` Spiele".

    Kein Treffer / keine Identitaet -> None (der Aufrufer meldet das als
    "nicht gefunden")."""
    ident = (cfg.me or "").strip()
    if not ident:
        log("[history] Keine Identitaet (me:) - Retry nicht moeglich.")
        return None
    client = _region_client(cfg)
    puuid = fetch._resolve_puuid(client, ident, log=log)
    if not puuid:
        log(f"[history] Identitaet '{ident}' nicht aufloesbar - Retry "
            f"abgebrochen.")
        return None
    ids = _window_ids(client, puuid, date_ms, log=log) if date_ms else []
    if not ids:
        if date_ms:
            log(f"[history] Zeitfenster leer - Rueckfall auf die letzten "
                f"{lookback} Spiele.")
        ids = client.match_ids(puuid, queue=None, count=lookback,
                               type_filter=None) or []
    if not ids:
        log("[history] Keine Match-IDs in der Match-History gefunden.")
        return None

    champs = frozenset(c for c in (roster or []) if c)
    best_id, best_delta = None, None
    for mid in ids:
        match = enrich._load_match(cfg, client, mid)
        if match is None:
            continue
        if champs and enrich._roster_matches(champs, match):
            log(f"[history] Spiel ueber das Roster gefunden: {mid}")
            return mid
        if date_ms:
            end = _match_end_ms(match)
            if end is None:
                continue
            delta = abs(end - int(date_ms))
            if (delta <= RETRY_TIME_TOLERANCE_MS
                    and _played_champ(match, puuid, champ, ident)
                    and (best_delta is None or delta < best_delta)):
                best_id, best_delta = mid, delta
    if best_id:
        log(f"[history] Spiel ueber Zeitstempel+Champion gefunden: {best_id}")
    return best_id


def _rebuild_report(cfg, match_id: str, path: Path, rec: dict, log=print,
                    real_id: str | None = None) -> str:
    """Den Report unter `path` aus dem vollen Timeline-Pfad neu schreiben.

    Der HTML-Dateiname bleibt in der Regel exakt derselbe (die History-Zeile
    verlinkt ihn), nur der Inhalt wird durch den vollstaendigen Report der
    ECHTEN Match-ID ersetzt. Der Trend-Record wird ueber `source_match_id` an
    diese Datei gebunden - so bleibt die History-Zeile joinbar und der alte
    Stempel-Record verschwindet (kein Doppelzaehlen im Trend).

    **Merge** (Bugfix 2026-07-30): Deckt bereits ein ANDERER Report dasselbe
    Spiel ab (Doppel-Capture), bleibt am Ende genau eine Datei uebrig:

      * der Zwilling ist vollstaendig -> diese Datei + ihr Stamm-Record werden
        geloescht, es wird NICHTS neu gebaut (kein API-Call);
      * beide unvollstaendig -> es wird in die Datei unter der echten Match-ID
        gebaut (sonst in diese hier), danach fliegen alle uebrigen Duplikate.

    `real_id` uebersteuert die Aufloesung (der manuelle GameID-Load kennt die
    Match-ID bereits). Rueckgabe = Registry-Zustand."""
    from app import postgame
    from .postgame import render

    date_ms = rec.get("date_ms")
    if not date_ms:
        try:
            date_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            date_ms = None
    if real_id:
        log(f"[history] Match-ID {real_id} vorgegeben - Resolver uebersprungen.")
    elif _REAL_MATCH_ID.match(path.stem):
        # Der Dateiname IST schon die Match-ID (Report aus `pipeline postgame`
        # statt aus einem Live-Capture) - dann ist nichts zu suchen. Spart den
        # Resolver samt seiner API-Calls und macht solche Reports auch dann
        # nachladbar, wenn gar kein Trend-Record dazu existiert (ohne Record
        # gaebe es weder Roster noch Champion, der Resolver liefe ins Leere).
        real_id = path.stem
        log(f"[history] Datei-Stamm {real_id} ist bereits eine Match-ID - "
            f"Resolver uebersprungen.")
    else:
        roster, champ = rec.get("roster"), rec.get("champ")
        if not roster:
            # Record-lose Datei (ihr Record wurde von einem anderen Lauf
            # verdraengt, s. trend._drop_displaced_stamp): die Suchmerkmale
            # stehen in der HTML selbst - sonst waere diese Zeile nie heilbar.
            roster, sniffed = _sniff_roster(path)
            champ = champ or sniffed
            if roster:
                log(f"[history] Kein Roster im Record - {len(roster)} Champions "
                    f"aus {path.name} gelesen.")
        real_id = resolve_match_id(cfg, roster=roster, date_ms=date_ms,
                                   champ=champ, log=log)
    if not real_id:
        log(f"[history] Kein passendes Spiel zu {match_id} in der "
            f"Match-History gefunden.")
        return "failed:nicht gefunden"

    try:
        index = _record_index(cfg)
    except Exception as exc:   # noqa: BLE001 - Records sind optional
        log(f"[history] Trend-Records nicht lesbar ({exc!r}).")
        index = {}
    dupes = _duplicate_files(cfg, real_id, path, index)
    if any(_report_status(p, index) == "ok" for p in dupes):
        # Das Spiel liegt bereits vollstaendig vor - diese Zeile ist ein
        # Doppel-Capture und verschwindet einfach (kein Neubau, keine Quota).
        log(f"[history] {path.name} ist ein Duplikat von {real_id} - der "
            f"vollstaendige Report bleibt, diese Datei wird entfernt.")
        _drop_report(cfg, path, real_id, log=log)
        return "done"

    # Beide unvollstaendig: die Datei unter der echten Match-ID ist der bessere
    # Zielort (stabiler Name), sonst bleibt es bei dieser Datei.
    real_file = cfg.postgame_out_dir / f"{real_id}.html"
    target = real_file if real_file in dupes else path

    report = postgame.build_report(cfg, real_id, me=(cfg.me or None), log=log)
    # Datei-Stempel behalten (URL bleibt stabil), Record trotzdem unter der
    # echten ID fuehren - s. trend.extract_record/write_record.
    report["source_match_id"] = target.stem
    report["enriched_match_id"] = real_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render.render_html(report), encoding="utf-8")
    trend.write_record(cfg, report, log=log)
    log(f"[history] Report {target.name} aus Match {real_id} neu befuellt.")
    for dupe in [*dupes, path]:
        if dupe != target:
            _drop_report(cfg, dupe, real_id, log=log)
    return "done"


def _spawn_daemon(fn) -> None:
    """Default-Spawner: Daemon-Thread (nicht-blockierend, stirbt mit dem
    Prozess). Tests injizieren einen synchronen Spawner."""
    threading.Thread(target=fn, daemon=True).start()


def start_retry(cfg, match_id: str, *, spawn=None, log=print) -> dict:
    """Retry fuer einen Report anstossen (asynchron).

    Rueckgabe sofort: `{"started": True}` bzw. `{"started": False, "reason":
    ...}` (nie ein HTTP-Fehler - der Aufrufer zeigt den Grund einfach an). Ein
    zweiter Trigger fuer dasselbe Spiel wird ignoriert, solange der erste laeuft.
    Der Thread selbst faengt ALLES ab und hinterlaesst nur einen Registry-Eintrag
    ("done" / "failed:<grund>")."""
    spawn = spawn or _spawn_daemon
    # Kein Pfad-Ausbruch ueber den Namen (der Wert kommt aus der URL).
    if not match_id or match_id != Path(match_id).name or match_id.startswith("."):
        return {"started": False, "reason": "Ungültige Match-ID."}
    if not cfg.active_api_keys:
        return {"started": False,
                "reason": "Kein API-Key konfiguriert (config.yml)."}
    if not (cfg.me or "").strip():
        return {"started": False,
                "reason": "Keine Identität konfiguriert (me: in config.yml)."}
    path = cfg.postgame_out_dir / f"{match_id}.html"
    if not path.exists():
        return {"started": False, "reason": "Report-Datei nicht gefunden."}
    with _RETRY_LOCK:
        if _RETRY_STATE.get(match_id) == "running":
            return {"started": False, "reason": "Retry läuft bereits."}
        _RETRY_STATE[match_id] = "running"

    rec = _record_index(cfg).get(match_id) or {}

    log(f"[history] Retry fuer {match_id} gestartet ...")
    spawn(lambda: _retry_once(cfg, match_id, path, rec, log=log))
    return {"started": True}


def _retry_once(cfg, match_id: str, path: Path, rec: dict, log=print,
                real_id: str | None = None) -> str:
    """Ein Spiel neu bauen und den Registry-Zustand hinterlassen.

    Faengt ALLES ab (auch SystemExit aus dem Riot-Client) - der Worker-Thread
    darf nichts nach aussen werfen. Rueckgabe = gesetzter Zustand."""
    try:
        state = _rebuild_report(cfg, match_id, path, rec, log=log,
                                real_id=real_id)
    except (Exception, SystemExit) as exc:   # noqa: BLE001 - nie crashen
        log(f"[history] Retry {match_id} fehlgeschlagen ({exc!r}).")
        state = f"failed:{exc}"
    _set_retry_state(match_id, state)
    return state


# ============================================================================
# 4b. Manuelles Nachladen ueber die GameID aus dem Riot-Client
# ============================================================================

def start_load(cfg, platform: str, game_id: str, *, spawn=None,
               log=print) -> dict:
    """Ein Spiel ueber Platform + GameID nachladen (asynchron).

    Der Riot-Client zeigt nur die numerische GameID - die Match-ID ist
    `<PLATFORM>_<gameid>`. Damit braucht dieser Weg WEDER Capture noch Resolver:
    die Region kommt aus der Match-ID (`fetch.routing_of`), das eigene Team
    faellt ohne aufloesbares `me:` auf Team 100 zurueck (s. postgame.build_report)
    - eine Identitaet ist also nicht zwingend.

    Existiert das Spiel schon:
      * vollstaendig -> `{"started": False, "reason": ..., "url": ...}` (kein
        API-Call, die Seite verlinkt einfach den vorhandenen Report);
      * unvollstaendig -> Retry auf GENAU DIESE Datei (Merge-Logik in
        `_rebuild_report` raeumt etwaige Duplikate mit weg).

    Rueckgabe sofort `{"started": True, "match_id": ...}` bzw. `{"started":
    False, "reason": ...}`; der Fortschritt kommt ueber `retry_state(match_id)`."""
    spawn = spawn or _spawn_daemon
    plat = (platform or "").strip().lower()
    gid = (game_id or "").strip()
    if plat not in fetch._PLATFORM_ROUTING:
        return {"started": False,
                "reason": f"Unbekannte Platform '{platform}'."}
    if not gid.isdigit():
        return {"started": False,
                "reason": "GameID muss eine reine Zahl sein."}
    if not cfg.active_api_keys:
        return {"started": False,
                "reason": "Kein API-Key konfiguriert (config.yml)."}

    mid = f"{plat.upper()}_{gid}"
    try:
        index = _record_index(cfg)
    except Exception as exc:   # noqa: BLE001 - Records sind optional
        log(f"[history] Trend-Records nicht lesbar ({exc!r}).")
        index = {}
    path = _existing_report(cfg, mid, index)
    if path is not None:
        if _report_status(path, index) == "ok":
            return {"started": False,
                    "reason": "Match ist schon vollständig vorhanden.",
                    "url": f"/reports/{path.name}"}
        log(f"[history] {mid} liegt unvollstaendig als {path.name} vor - "
            f"wird neu befuellt.")
    else:
        path = cfg.postgame_out_dir / f"{mid}.html"

    with _RETRY_LOCK:
        if _RETRY_STATE.get(mid) == "running":
            return {"started": False, "reason": "Laden läuft bereits."}
        _RETRY_STATE[mid] = "running"

    rec = index.get(path.stem) or {}
    log(f"[history] Laden von {mid} gestartet ...")
    spawn(lambda: _retry_once(cfg, mid, path, rec, log=log, real_id=mid))
    return {"started": True, "match_id": mid}


# ============================================================================
# 5. Sammel-Retry: alle unvollstaendigen Reports in EINEM Rutsch nachladen
# ============================================================================

def _retry_targets(cfg, *, limit: int = DEFAULT_LIMIT, log=print) -> list[str]:
    """IDs aller Reports, die aktuell einen Retry vertragen.

    Exakt die Zeilen, die die History-Seite als retrybar zeigt (`_row`:
    Status failed/no_key/unknown, haengendes pending oder "ok" ohne bekannten
    Ausgang, Key vorhanden) - ohne die, an denen gerade schon ein Retry
    arbeitet."""
    games = list_games(cfg, limit=limit, log=log)["games"]
    return [g["match_id"] for g in games
            if g["retryable"] and not g["retry_running"]]


def start_retry_all(cfg, *, spawn=None, sleep=time.sleep, log=print) -> dict:
    """Alle retrybaren Reports nacheinander nachladen (asynchron).

    Rueckgabe sofort: `{"started": <anzahl>}` bzw. `{"started": 0, "reason":
    ...}`. EIN Daemon-Thread arbeitet die Spiele SEQUENZIELL ab (kein
    Parallel-Feuern gegen das Rate-Limit), mit `RETRY_BATCH_PAUSE_S` Pause
    dazwischen; `sleep` ist fuer Tests injizierbar.

    Quota: je Spiel genau ein Zeitfenster-Request auf die Match-IDs (s.
    `_window_ids`) plus die Matches, die der Disk-Cache (`enrich._load_match`)
    noch nicht hat - Spiele desselben Abends teilen sich diese Matches also
    ueber den Cache.

    Doppel-Trigger: ein zweiter Batch wird abgelehnt, solange der erste laeuft;
    die betroffenen Spiele stehen ausserdem sofort auf "running", damit ein
    Einzel-Retry auf ein eingeplantes Spiel abgewiesen wird (und die Seite den
    Fortschritt je Zeile zeigt)."""
    global _BATCH_RUNNING
    spawn = spawn or _spawn_daemon
    if not cfg.active_api_keys:
        return {"started": 0,
                "reason": "Kein API-Key konfiguriert (config.yml)."}
    if not (cfg.me or "").strip():
        return {"started": 0,
                "reason": "Keine Identität konfiguriert (me: in config.yml)."}
    with _BATCH_LOCK:
        if _BATCH_RUNNING:
            return {"started": 0, "reason": "Sammel-Retry läuft bereits."}
        _BATCH_RUNNING = True

    # Ab hier MUSS das Flag auf jedem Pfad wieder fallen.
    try:
        targets = _retry_targets(cfg, log=log)
    except Exception as exc:   # noqa: BLE001 - nie crashen
        _set_batch_running(False)
        log(f"[history] Sammel-Retry nicht startbar ({exc!r}).")
        return {"started": 0, "reason": str(exc)}
    jobs = []
    with _RETRY_LOCK:
        for mid in targets:
            path = cfg.postgame_out_dir / f"{mid}.html"
            if not path.exists() or _RETRY_STATE.get(mid) == "running":
                continue
            _RETRY_STATE[mid] = "running"
            jobs.append((mid, path))
    if not jobs:
        _set_batch_running(False)
        return {"started": 0, "reason": "Kein Report zum Nachladen."}

    try:
        index = _record_index(cfg)
    except Exception as exc:   # noqa: BLE001 - Records sind optional
        log(f"[history] Trend-Records nicht lesbar ({exc!r}).")
        index = {}

    def _work() -> None:
        try:
            for i, (mid, path) in enumerate(jobs):
                # Zustand direkt vor dem Spiel (nochmal) setzen - so gilt
                # "running" auch dann, wenn jemand die Registry zwischendurch
                # geleert hat.
                _set_retry_state(mid, "running")
                _retry_once(cfg, mid, path, index.get(mid) or {}, log=log)
                if i + 1 < len(jobs):
                    sleep(RETRY_BATCH_PAUSE_S)
        finally:
            _set_batch_running(False)
            log(f"[history] Sammel-Retry beendet ({len(jobs)} Spiele).")

    log(f"[history] Sammel-Retry fuer {len(jobs)} Spiele gestartet ...")
    try:
        spawn(_work)
    except Exception as exc:   # noqa: BLE001 - kein Thread, kein Batch
        _set_batch_running(False)
        for mid, _path in jobs:
            _set_retry_state(mid, f"failed:{exc}")
        log(f"[history] Sammel-Retry nicht startbar ({exc!r}).")
        return {"started": 0, "reason": str(exc)}
    return {"started": len(jobs)}
