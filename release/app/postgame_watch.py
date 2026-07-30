"""Auto-Trigger fuer den Post-Game-Report (Phase 3 Teil C, s. plan_postgame.md
§3/§6).

EIN Poll-Loop im laufenden Server fuettert die Live-Rohdaten inkrementell in ein
In-Memory-Capture (`app.postgame.capture.LiveCapture`) und erkennt das
Spielende (Port 2999 liefert kein `allgamedata` mehr). Danach **zweistufig**:

  1. **Stufe 1 - sofort, key-frei:** der Report wird aus den In-Memory-Serien
     gerendert (kein Key, `me` = activePlayer, kein Warten auf Riot-Indexierung).
  2. **Stufe 2 - asynchron, nur mit Key:** die echte Match-ID ueber den
     Roster-Abgleich finden (Retry/Backoff bis indexiert) und DIESELBE HTML-Datei
     durch den VOLLEN Match-V5-Report ersetzen (Upgrade in place).

Stufe 2 merged seit 2026-07-30 nichts mehr, sondern baut den Report komplett neu
(`postgame.build_report`): die Live-Client-API liefert einzelne Felder zeitweise
falsch (real bei Viego: leere Item-Listen -> Item-Gold-Kurve fiel auf 0), Match-V5
ist die verlaessliche Quelle. Der Datei-Stempel bleibt (`source_match_id`), damit
Link und History-Zeile stabil sind.

Das ersetzt den alten Key-Fetch-Trigger aus Phase 2 (Baseline-Polling per
Key-Fetch -> `postgame.run`). **Gating NEU:** nur noch
`postgame.auto_on_end` + nicht-Demo + SR-Spiel. Key/`me:` sind KEINE
Voraussetzung mehr - ohne Key gibt es Stufe 1 trotzdem.

Robustheit vor allem: die Transitions-/Trigger-Logik steckt in
`PostgameWatcher`, ist rein zustandsbasiert + injizierbare Seams (Capture,
Render-/Enrich-Callbacks, Spawner) und damit ohne Thread/Netz testbar. Die
netz-/zeit-intensive Stufe 2 laeuft ueber den `spawn`-Seam (Default:
Daemon-Thread); jeder Schritt ist in try/except gekapselt und darf den Server
niemals crashen.
"""

import threading
import time
from datetime import datetime


# Verzoegerung (Sekunden) fuer den EINEN spaeten Zweitversuch, falls Stufe 2 ihr
# Retry-Budget erschoepft hat. Riot indexiert manche Matches erst deutlich
# spaeter (real gesehen >5 min, manueller Fetch ~15 min spaeter erfolgreich) -
# nach einer Pause bekommt der Report darum noch einen einzigen Anlauf.
_RETRY_DELAY = 600.0


def _spawn_daemon(fn) -> None:
    """Default-Spawner: fuehrt `fn` in einem Daemon-Thread aus (nicht-blockierend,
    stirbt mit dem Prozess). Tests injizieren einen synchronen Spawner."""
    threading.Thread(target=fn, daemon=True).start()


def _is_sr(raw: dict) -> bool:
    """True, wenn der laufende Poll ein Summoner's-Rift-5v5-Spiel ist.

    Nur SR (CLASSIC / mapNumber 11) wird gecapturet - andere Queues (ARAM, Demo,
    Arena) erzeugen keinen Report (s. plan_postgame.md §7)."""
    gd = (raw.get("gameData", {}) or {})
    return gd.get("gameMode") == "CLASSIC" or gd.get("mapNumber") == 11


def _default_render_stage1(cfg, result, out_path, log) -> None:
    """Stufe 1: key-freien Report aus dem Capture rendern (kein Key noetig).

    `status_override="pending"` setzt den zustandsbewussten Disclaimer auf "wird
    nachgeladen" - aber NUR, wenn ein Key da ist (der Report-Kern schlaegt ohne
    Key auf `no_key` um). So sieht der Nutzer bei laufender Stufe 2 den ehrlichen
    "wird automatisch aktualisiert"-Text statt eines falschen "kein Key"."""
    from app import postgame
    path = postgame.run_from_capture(cfg, result, enrich_damage=False,
                                     status_override="pending",
                                     out_path=out_path, log=log)
    log(f"[postgame] Stufe 1 (key-frei) geschrieben: {path}")


def _sync_trend_record(cfg, report, log) -> None:
    """Trend-Record auf den Stand des gerade geschriebenen Reports bringen.

    Stufe 1 hat den Record bereits angelegt (`run_from_capture`); jedes spaetere
    In-place-Neuschreiben der HTML-Datei (Stufe-2-Upgrade, endgueltiges
    Scheitern) muss ihn nachziehen, sonst behaelt der Trend/die Match-History den
    veralteten Stand (win=None, has_damage=False, damage_status="pending").

    `trend.write_record` ist idempotent und raeumt den stale Stempel-Record auf:
    kennt der Report inzwischen die echte Match-ID (`enriched_match_id`), laeuft
    der Record unter ihr und das alte `live_<...>.json` wird geloescht - so
    zaehlt die Trend-Aggregation ein Spiel nie doppelt. Vollstaendig in
    try/except (write_record schluckt zwar selbst, aber der Aufruf soll unter
    keinen Umstaenden die Stufe-2-Kette brechen)."""
    from app.postgame import trend
    try:
        trend.write_record(cfg, report, log=log)
    except Exception as exc:   # noqa: BLE001 - Record darf nie den Report brechen
        log(f"[postgame] Trend-Record nicht aktualisiert ({exc!r}).")


def _default_write_failed(cfg, result, out_path, log) -> None:
    """Endgueltiges Scheitern von Stufe 2: DIESELBE Datei einmalig mit dem
    failed-Disclaimer neu schreiben (derselbe Mechanismus wie das Erfolgs-Upgrade,
    nur mit `status_override="failed"` statt Match-Daten). Ohne Online-Versuch
    (enrich_damage=False) - es wurde bereits erschoepfend versucht; hier geht es
    nur noch um den ehrlichen Hinweis inkl. manuellem Fallback-Befehl."""
    from app import postgame
    from app.postgame import render
    report = postgame.build_report_from_capture(cfg, result, enrich_damage=False,
                                                status_override="failed", log=log)
    out_path.write_text(render.render_html(report), encoding="utf-8")
    _sync_trend_record(cfg, report, log)
    log(f"[postgame] Stufe 2 endgueltig gescheitert - failed-Report "
        f"geschrieben: {out_path}")


def _write_pending_progress(cfg, result, out_path, attempt, retries, is_retry,
                            log) -> None:
    """Zwischenstand nach einem Fehlversuch: DIESELBE Datei mit dem frisch
    gebauten (key-freien) Capture-Report + echtem Versuchszaehler neu schreiben.

    So zeigt der Status-Chip oben im Header "Versuch X/Y" statt eines statischen
    "wird nachgeladen" - der Nutzer sieht, dass wirklich noch etwas laeuft.
    Bewusst `build_report_from_capture` + `render.render_html` direkt statt
    `run_from_capture`: letzteres wuerde bei jedem Zwischenschritt erneut einen
    Trend-Record schreiben. `enrich_damage=False` haelt den Zwischenstand rein
    lokal - der Online-Versuch laeuft in der Schleife selbst.

    `damage_status` wird zusaetzlich hart auf "pending" gesetzt - in diesem
    Codepfad ist immer ein Key aktiv (ohne Key laeuft Stufe 2 gar nicht), die
    "no_key gewinnt"-Regel aus `build_report_from_capture` wird also nicht
    verletzt. Vollstaendig in try/except: ein Bau-/Schreibfehler darf die
    Retry-Schleife NIE abbrechen."""
    from app import postgame
    from app.postgame import render
    try:
        report = postgame.build_report_from_capture(
            cfg, result, enrich_damage=False, status_override="pending", log=log)
        report["damage_status"] = "pending"
        report["enrich_progress"] = {"attempt": attempt, "retries": retries,
                                     "is_retry": is_retry}
        out_path.write_text(render.render_html(report), encoding="utf-8")
    except Exception as exc:
        log(f"[postgame] Stufe-2-Zwischenstand ({attempt}/{retries}) nicht "
            f"schreibbar ({exc}) - Schleife laeuft weiter.")


def _default_enrich_stage2(cfg, result, out_path, log,
                           retries: int | None = None,
                           backoff: float | None = None,
                           is_retry: bool = False) -> bool:
    """Stufe 2: mit Retry/Backoff den VOLLEN Match-V5-Report nachziehen.

    Je Versuch: die echte Match-ID ueber den Roster-Abgleich suchen
    (`enrich.find_match_id`) und daraus den kompletten Timeline-Report bauen
    (`postgame.build_report`) - der ersetzt den key-freien Stufe-1-Report in
    DERSELBEN Datei. `source_match_id` haelt den Datei-Stempel fest (Link/
    History-Zeile bleiben stabil), `enriched_match_id` traegt die echte ID, unter
    der der Trend-Record laeuft (`trend.write_record` raeumt den stale
    Stempel-Record dabei weg).

    Match-V5 braucht nach Spielende etwas, bis das Match indexiert ist - und
    zwischen "Match da" und "Timeline da" liegt nochmal ein Fenster (dann wirft
    `build_report` SystemExit). BEIDES zaehlt nur als EIN gescheiterter Versuch:
    Zwischenstand mit echtem Versuchszaehler schreiben, Backoff, weiter. Ist das
    Budget erschoepft, bleibt der zuletzt geschriebene pending-Report stehen.

    Budget kommt aus der Config (`postgame.enrich_retries` /
    `enrich_backoff_seconds`); Tests injizieren `retries`/`backoff` direkt.
    `is_retry` markiert den spaeten Zweitversuch (nur fuer die Anzeige).
    Rueckgabe: True bei erfolgreicher Aufwertung, sonst False (Budget
    erschoepft / kein Roster-Treffer)."""
    from app import postgame
    from app.postgame import enrich, render
    if retries is None:
        retries = cfg.postgame_enrich_retries
    if backoff is None:
        backoff = cfg.postgame_enrich_backoff_seconds
    # Identitaet: config `me:` > activePlayer des Captures (im Live-Betrieb ist
    # der activePlayer der Nutzer).
    ident = (cfg.me or "").strip() or result.me_ident
    for attempt in range(1, retries + 1):
        try:
            real_id = enrich.find_match_id(cfg, result.pid_map, ident, log=log)
            if real_id:
                report = postgame.build_report(cfg, real_id, me=ident, log=log)
                # Datei-Stempel behalten (URL/History-Zeile bleiben stabil),
                # Record trotzdem unter der echten ID fuehren.
                report["source_match_id"] = out_path.stem
                report["enriched_match_id"] = real_id
                out_path.write_text(render.render_html(report), encoding="utf-8")
                # Record nachziehen: erst jetzt sind Sieg/Niederlage, Schaden und
                # die echte Match-ID bekannt (s. _sync_trend_record).
                _sync_trend_record(cfg, report, log)
                log(f"[postgame] Stufe 2: Report aus Match {real_id} vollstaendig "
                    f"neu gebaut -> {out_path}")
                return True
        except (Exception, SystemExit) as exc:   # noqa: BLE001 - nie crashen
            # Typischer Fall: Match indexiert, Timeline noch 404 (SystemExit aus
            # build_report). Ein Fehlversuch, kein Abbruch der Stufe.
            log(f"[postgame] Stufe-2-Versuch {attempt}/{retries} fehlgeschlagen "
                f"({exc}) - wird erneut probiert.")
        if attempt < retries:
            _write_pending_progress(cfg, result, out_path, attempt, retries,
                                    is_retry, log)
            time.sleep(backoff)
    log("[postgame] Stufe 2: Match nicht rechtzeitig indexiert / kein "
        "Roster-Treffer - Report bleibt key-frei.")
    return False


class PostgameWatcher:
    """Erkennt Spielende (aktiv->kein Spiel) im Live-Poll und stoesst den
    zweistufigen Auto-Report an - genau einmal pro Spielende.

    Ablauf pro Spiel: jeder aktive SR-Poll fuettert das Capture; die Transition
    'aktiv -> None' friert das Capture ein (race-frei fuer die asynchrone Stufe
    2) und spawnt den Report. Gating: laeuft mit gesetztem Flag ausserhalb des
    Demo-Modus; SR-Filter pro Poll. Key/`me` sind KEINE Voraussetzung mehr."""

    def __init__(self, cfg, *, capture=None, render_stage1=None,
                 enrich_stage2=None, write_failed=None, spawn=None, log=print,
                 demo=False, retry_delay_seconds=None, sleep=None):
        self.cfg = cfg
        self._log = log
        self._demo = demo
        self._spawn = spawn or _spawn_daemon
        self._sleep = sleep or time.sleep
        self._retry_delay = (_RETRY_DELAY if retry_delay_seconds is None
                             else retry_delay_seconds)
        self._render_stage1 = render_stage1 or _default_render_stage1
        self._enrich_stage2 = enrich_stage2 or _default_enrich_stage2
        self._write_failed = write_failed or _default_write_failed
        if capture is None:
            from app.postgame.capture import LiveCapture
            capture = LiveCapture()
        self._capture = capture
        self._was_active = False   # ob der letzte relevante Poll ein Spiel sah
        # Letzter geschriebener Report, thread-sicher fuer /api/state lesbar
        # (der Poll-/Stufe-2-Thread schreibt, der Frontend-Handler liest).
        # None = noch kein Report; sonst {"path": Path, "written_at": iso-str,
        # "enriched": bool}. `enriched` flippt auf True, sobald Stufe 2 die Datei
        # durch den vollen Match-V5-Report ersetzt hat.
        self._report_lock = threading.Lock()
        self._last_report = None

    # --- Gating -------------------------------------------------------------
    @property
    def disabled_reason(self) -> str | None:
        """Grund, warum der Auto-Trigger NICHT laeuft, oder None (aktiv). Genau
        eine erklaerende Log-Zeile beim Start - nicht pro Poll spammen. Key/`me:`
        gehen hier NICHT mehr ein (Stufe 1 laeuft key-frei)."""
        if self._demo:
            return "Demo-Modus (kein echtes Match)"
        if not self.cfg.postgame_auto_on_end:
            return "postgame.auto_on_end: false"
        return None

    @property
    def enabled(self) -> bool:
        return self.disabled_reason is None

    # --- Report-Zustand (thread-sicher) -------------------------------------
    def _set_report(self, out_path, *, enriched: bool) -> None:
        """Den zuletzt geschriebenen Report vermerken. `written_at` wird bei der
        ERSTEN Verfuegbarkeit (Stufe 1) gesetzt; das Stufe-2-Upgrade schreibt
        DIESELBE Datei neu, behaelt aber den urspruenglichen Zeitpunkt und flippt
        nur `enriched`. Unter dem Report-Lock, damit der lesende Frontend-Handler
        immer einen konsistenten Eintrag sieht."""
        with self._report_lock:
            prev = self._last_report
            if prev is not None and prev["path"] == out_path:
                written_at = prev["written_at"]
            else:
                written_at = datetime.now().astimezone().isoformat()
            self._last_report = {"path": out_path, "written_at": written_at,
                                 "enriched": enriched}

    def last_report(self) -> dict | None:
        """Thread-sicherer Lese-Zugriff fuer /api/state. Liefert None ohne Report
        ODER wenn die Datei inzwischen fehlt (manuell geloescht) - so wirft ein
        toter Link nie einen 500, sondern verschwindet einfach aus dem State.
        Sonst {"url": "/reports/<datei>", "written_at": <iso>, "enriched": bool}."""
        with self._report_lock:
            rep = self._last_report
            if rep is None:
                return None
            path, written_at, enriched = (rep["path"], rep["written_at"],
                                          rep["enriched"])
        if not path.exists():
            return None
        return {"url": f"/reports/{path.name}", "written_at": written_at,
                "enriched": enriched}

    # --- Poll-Verarbeitung --------------------------------------------------
    def observe(self, raw) -> None:
        """Einen Live-Poll verarbeiten. `raw` = `fetch_allgamedata()` (dict) oder
        None (kein Spiel). Aktive SR-Polls fuellen das Capture; die Transition
        'aktiv -> None' loest den Auto-Report aus. Bei deaktiviertem Trigger ein
        striktes No-Op; Nicht-SR-Spiele werden gar nicht gecapturet."""
        if not self.enabled:
            return
        active = bool(raw) and bool(raw.get("allPlayers"))
        if active and not _is_sr(raw):
            # Nicht-SR (ARAM/Arena/...): nicht capturen, nicht triggern.
            self._was_active = False
            return
        if active:
            try:
                self._capture.feed(raw)
            except Exception as exc:  # Capture darf den Poll-Loop nie crashen
                self._log(f"[postgame] Capture-Feed-Fehler ({exc})")
            self._was_active = True
            return
        # active is False -> ggf. Spielende.
        was = self._was_active
        self._was_active = False
        if was and self._capture.has_data:
            self._trigger()

    def _trigger(self) -> None:
        """Spielende: Capture einfrieren und den zweistufigen Report spawnen. Das
        Einfrieren passiert synchron auf dem Poll-Thread (bevor ein neues Spiel
        das Capture zuruecksetzt); die eigentliche Arbeit laeuft ueber `spawn`."""
        try:
            result = self._capture.freeze()
        except Exception as exc:  # freeze darf den Poll-Loop nie crashen
            self._log(f"[postgame] Capture-Einfrieren fehlgeschlagen ({exc})")
            return
        if result is None:
            return
        if not result.supported:
            self._log("[postgame] Spielende erkannt, aber kein SR-Spiel - kein "
                      "Report.")
            return
        self._spawn(lambda: self._finish(result))

    def _finish(self, result) -> None:
        """Stufe 1 (key-frei, sofort) rendern, danach ggf. Stufe 2 (mit Key,
        asynchron) spawnen. Vollstaendig in try/except - jeder Fehler bleibt eine
        Warnung und beeintraechtigt den Server nie."""
        out_path = self.cfg.postgame_out_dir / f"{result.match_id}.html"
        try:
            self._render_stage1(self.cfg, result, out_path, self._log)
        except (Exception, SystemExit) as exc:
            self._log(f"[postgame] Stufe-1-Report fehlgeschlagen ({exc})")
            return
        self._set_report(out_path, enriched=False)
        self._log(f"[postgame] Report bereit: {out_path} "
                  f"(oeffne die Datei im Browser)")
        if self.cfg.active_api_keys:
            self._spawn(lambda: self._run_stage2(result, out_path))
        else:
            self._log("[postgame] Kein API-Key - Report bleibt key-frei "
                      "(keine Stufe 2).")

    def _run_stage2(self, result, out_path, *, is_retry: bool = False) -> None:
        """Stufe 2 gekapselt: das Report-Upgrade darf den Server nie crashen.

        Gibt Stufe 2 auf (Budget erschoepft / kein Roster-Treffer) und ist dies
        der ERSTE Anlauf, wird EIN verzoegerter Zweitversuch eingeplant (Riot
        indexiert manche Matches schlicht spaeter). Der Zweitversuch (`is_retry`)
        gibt bei erneutem Fehlschlag endgueltig auf - keine Endlosschleife."""
        try:
            # `is_retry` als Keyword durchgereicht - der Seam muss es annehmen
            # (Default-Impl. und Test-Fakes tun das ueber **kw).
            enriched = bool(self._enrich_stage2(self.cfg, result, out_path,
                                                self._log, is_retry=is_retry))
        except (Exception, SystemExit) as exc:
            self._log(f"[postgame] Stufe-2-Upgrade fehlgeschlagen ({exc}) - "
                      f"Report bleibt key-frei.")
            enriched = False
        if enriched:
            # Datei wurde in place durch den vollen Match-Report ersetzt ->
            # Report-Zustand nachziehen, damit der Frontend-Button die Badge zeigt.
            self._set_report(out_path, enriched=True)
            return
        if is_retry:
            # Auch der spaete Zweitversuch scheiterte -> endgueltig aufgeben.
            # Den (bis hierhin "pending") Report EINMALIG mit dem failed-Disclaimer
            # neu schreiben, damit kein irrefuehrender "wird nachgeladen"-Text
            # stehen bleibt. `enriched` bleibt False -> last_report()-Info korrekt.
            try:
                self._write_failed(self.cfg, result, out_path, self._log)
            except (Exception, SystemExit) as exc:
                self._log(f"[postgame] failed-Report schreiben fehlgeschlagen "
                          f"({exc}) - Report bleibt unveraendert.")
            self._log("[postgame] Report-Aufwertung nicht moeglich - manuell: "
                      "uv run python -m pipeline postgame --latest")
            return
        # Erster Anlauf gescheitert -> genau EINEN spaeten Zweitversuch planen.
        self._schedule_retry(result, out_path)

    def _schedule_retry(self, result, out_path) -> None:
        """Einen einzigen verzoegerten Stufe-2-Zweitversuch ueber den spawn-Seam
        einplanen. `result`/`out_path` sind das eingefrorene, entkoppelte
        Capture-Buendel dieses Spielendes - ein inzwischen gestartetes NEUES Spiel
        veraendert sie nicht, der Zweitversuch wertet also weiterhin den ALTEN
        Report auf. Vollstaendig in try/except: darf den Server nie crashen."""
        self._log(f"[postgame] Stufe 2 aufgegeben - verzoegerter Zweitversuch in "
                  f"{int(self._retry_delay)} s eingeplant.")

        def _delayed() -> None:
            try:
                self._sleep(self._retry_delay)
                self._run_stage2(result, out_path, is_retry=True)
            except (Exception, SystemExit) as exc:
                self._log(f"[postgame] Verzoegerter Zweitversuch fehlgeschlagen "
                          f"({exc}).")

        self._spawn(_delayed)
