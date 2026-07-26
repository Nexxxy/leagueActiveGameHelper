"""In-Memory-Capture der Spiel-Zeithistorie (Phase 3 Teil C).

Statt den Live-Client-Dump von Platte zu lesen (`live_series` ueber einen
Dump-Ordner), fuettert der laufende Server jeden Poll direkt in eine
`LiveCapture`. Die Klasse baut **inkrementell** dieselben Minuten-Serien +
Event-Stroeme auf wie der Datei-Pfad - nur im Speicher (Kilobytes), ohne
Roh-Snapshots anzusammeln und ohne Disk-I/O.

**Paritaet ist Pflicht:** die pro-Snapshot-Extraktion und der Serien-Assembler
sind bewusst mit dem Datei-Pfad geteilt (`live_series.extract_snapshot`/
`assemble_players`/`build_events_from_list`/...). Die dump_min-Snapshots einzeln
durch `LiveCapture.feed()` geschoben ergeben dieselben Serien wie
`live_series.build_series_from_dump` ueber den Ordner (s. Test-Paritaet).

Kern-Eigenschaften der Live-API (verifiziert, s. plan_postgame.md §2.2b):
  * `raw.events.Events` ist **kumulativ** - jeder Poll traegt ALLE bisherigen
    Events. Fuer den Event-Strom genuegt darum die Liste des letzten Polls.
  * Die Spieler-Serien muessen pro Minute gesampelt werden: Wert bei Minute M =
    letzter Poll mit gameTime <= M·60. Das repliziert `live_series._snapshot_at`
    exakt, ohne die Snapshots zu behalten (s. `_advance`-Kommentar unten).

Speicher: nur die Minuten-Extrakte (je Frame ein kleines pid->Werte-Dict), die
letzte Event-Liste, die letzten Endwerte und der Item-Namen-Lookup. Kein
Disk-Fallback (Absturz = Historie weg - akzeptierte Entscheidung).
"""

import time as _time

from . import live_series

# gameTime-Ruecksprung (Sekunden), ab dem ein NEUES Spiel angenommen wird. Ein
# normaler Poll erhoeht gameTime; ein deutlicher Ruecksprung heisst neue Lobby.
_RESET_DROP = 30.0

# Als Summoner's Rift gewertete Live-Modi (deckungsgleich mit live_series).
_SR_MODES = frozenset({"CLASSIC"})


def _game_time(raw: dict) -> float:
    return float((raw.get("gameData", {}) or {}).get("gameTime", 0.0) or 0.0)


def _game_mode(raw: dict) -> str:
    return (raw.get("gameData", {}) or {}).get("gameMode", "") or ""


class LiveCapture:
    """Inkrementeller Aufbau der Minuten-Serien + Events aus Live-Polls.

    Ablauf: `feed(raw)` pro Poll; bei Spielende ruft der Watcher `freeze()` fuer
    ein unveraenderliches Ergebnis-Buendel (dieselbe Serien-/Event-Struktur wie
    `live_series` aus einem Dump-Ordner) und `reset()` bzw. das naechste Spiel
    setzt das Capture automatisch neu (gameTime-Ruecksprung)."""

    def __init__(self):
        self.reset()

    # --- Zustand ------------------------------------------------------------
    def reset(self) -> None:
        """Alles verwerfen (neues Spiel / Start)."""
        self._pid_map: dict | None = None
        self._mode: str = ""
        self._me_ident: str | None = None
        self._started_at: str = ""       # Wall-Clock-Stempel des ersten Polls
        self._first_ext: dict | None = None   # Extrakt des ersten Polls (Frame-0-Fallback)
        self._minute_ext: list[dict] = []      # finalisierte Minuten-Extrakte
        self._latest_ext: dict | None = None   # Extrakt des letzten Polls
        self._latest_gt: float = -1.0          # gameTime des letzten Polls
        self._last_events: list = []           # letzte nicht-leere Events-Liste
        self._last_final: dict = {}            # Endwerte je pid (letzter Poll)
        self._item_names: dict = {}            # itemID -> displayName (kumuliert)

    @property
    def has_data(self) -> bool:
        """True, sobald mindestens ein Poll eingeflossen ist (Report moeglich)."""
        return self._pid_map is not None and self._latest_gt >= 0.0

    @property
    def supported(self) -> bool:
        """True, wenn der bisher gesehene Modus Summoner's Rift (CLASSIC) ist."""
        return self._mode in _SR_MODES

    @property
    def n_frames(self) -> int:
        """Aktuelle Minuten-Zahl (int(max_gt // 60) + 1), 0 ohne Daten."""
        if self._latest_gt < 0.0:
            return 0
        return int(self._latest_gt // 60) + 1

    # --- Poll-Verarbeitung --------------------------------------------------
    def feed(self, raw: dict) -> None:
        """Einen Live-Poll (`allgamedata`) einspeisen.

        Erkennt Spielwechsel (gameTime-Ruecksprung) und finalisiert die
        Minuten-Slots, deren Marke der neue Poll ueberschritten hat. Fehlt
        `allPlayers` (Ladebildschirm o. ae.), wird der Poll ignoriert."""
        if not raw or not raw.get("allPlayers"):
            return
        gt = _game_time(raw)

        # Spielwechsel: gameTime springt deutlich zurueck -> frisch anfangen.
        if self.has_data and gt < self._latest_gt - _RESET_DROP:
            self.reset()

        first = self._pid_map is None
        if first:
            self._pid_map = live_series.build_pid_map([raw])
            self._mode = _game_mode(raw)
            self._me_ident = live_series.resolve_me_name_from_raw(raw)
            self._started_at = _time.strftime("%Y%m%d_%H%M%S")

        ext = live_series.extract_snapshot(raw, self._pid_map)
        if first:
            # Frame-0-Fallback: Minuten vor dem ersten Poll zeigen den ersten
            # Poll (identisch zu live_series._snapshot_at, das dann snapshots[0]
            # zurueckgibt).
            self._first_ext = ext

        # Minuten-Slots finalisieren, deren Marke (M·60) der neue Poll passiert
        # hat. Vor dem Passieren traegt Slot M den letzten Poll mit gt <= M·60 -
        # das ist genau `_latest_ext` (bzw. `_first_ext` fuer die Frames vor dem
        # allerersten Poll). Damit ist `_snapshot_at` byte-genau repliziert,
        # ohne die Roh-Snapshots zu behalten.
        self._advance(gt)

        self._latest_ext = ext
        self._latest_gt = gt
        self._last_final = live_series.final_stats_from_raw(raw, self._pid_map)
        live_series.collect_item_names(raw, self._item_names)
        evs = ((raw.get("events", {}) or {}).get("Events", []) or [])
        if evs:
            self._last_events = evs

    def _advance(self, gt_new: float) -> None:
        """Finalisiert alle Minuten-Slots M mit M·60 < gt_new.

        Der jeweils zu buchende Wert ist der letzte Poll mit gt <= M·60 (=
        `_latest_ext`, vor dem ersten Poll `_first_ext`). Weil Polls streng
        aufsteigend kommen, ist ein einmal ueberschrittener Slot fuer immer
        fixiert."""
        while len(self._minute_ext) * 60 < gt_new:
            self._minute_ext.append(
                self._latest_ext if self._latest_ext is not None
                else self._first_ext)

    # --- Abschluss ----------------------------------------------------------
    def freeze(self) -> "CaptureResult | None":
        """Unveraenderliches Ergebnis-Buendel des aktuellen Spiels (oder None).

        Baut EINMAL die vollstaendigen Serien/Events/Endwerte aus dem
        akkumulierten Zustand. Trailing-Slots (letzter Poll auf exakter
        Minuten-Marke) werden hier aufgefuellt. Rueckgabe ist von der weiteren
        Capture-Nutzung entkoppelt - ein direkt danach startendes neues Spiel
        (Reset) kann das Buendel nicht mehr veraendern (race-frei fuer die
        asynchrone Stufe 2)."""
        if not self.has_data:
            return None
        pid_map = self._pid_map
        n_frames = self.n_frames
        # Trailing-Slots (nur wenn max_gt genau auf einer Minuten-Marke liegt)
        # mit dem letzten Poll fuellen - dann gilt gt == M·60 <= M·60.
        minute_ext = list(self._minute_ext)
        while len(minute_ext) < n_frames:
            minute_ext.append(self._latest_ext)

        players = live_series.assemble_players(minute_ext, pid_map)
        events = live_series.build_events_from_list(self._last_events, pid_map)
        ser = {"frame_interval": 60000, "n_frames": n_frames,
               "players": players, "events": events}
        return CaptureResult(
            pid_map=pid_map,
            ser=ser,
            finals={pid: dict(v) for pid, v in self._last_final.items()},
            id_to_name=live_series.name_lookup(dict(self._item_names)),
            mode=self._mode,
            me_ident=self._me_ident,
            duration_min=round(self._latest_gt / 60.0, 1),
            started_at=self._started_at,
        )


class CaptureResult:
    """Eingefrorenes, unveraenderliches Ergebnis eines Capture-Spiels.

    Traegt genau die Primitive, die `postgame.build_report_from_capture` braucht
    (dieselben, die der Datei-Pfad aus einem Dump-Ordner gewinnt). `match_id`
    ist der stabile Datei-Stamm des Spiels (`live_<startstempel>`), sodass Stufe
    1 und Stufe 2 dieselbe HTML-Datei schreiben."""

    def __init__(self, *, pid_map, ser, finals, id_to_name, mode, me_ident,
                 duration_min, started_at):
        self.pid_map = pid_map
        self.ser = ser
        self.finals = finals
        self.id_to_name = id_to_name
        self.mode = mode
        self.me_ident = me_ident
        self.duration_min = duration_min
        self.started_at = started_at

    @property
    def supported(self) -> bool:
        """True, wenn Summoner's Rift (CLASSIC)."""
        return self.mode in _SR_MODES

    @property
    def match_id(self) -> str:
        """Stabiler Datei-Stamm des Spiels (fuer beide Report-Stufen)."""
        return f"live_{self.started_at}" if self.started_at else "live_capture"
