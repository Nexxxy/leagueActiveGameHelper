"""Fortschritts-Objekt fuer lange Report-Neubauten (Transport fuer den Ladebalken).

WOFUER: Ein Report-Neubau (Stufe 2 nach Spielende, Retry/Fix/Laden in der
Match-History) haengt minutenlang im Riot-Rate-Limit, ohne dass der Nutzer
sieht, wie weit er ist. `Progress` sammelt den Stand als PHASE + ZAEHLER und
reicht ihn ueber `on_change` an die jeweilige Registry weiter (Watcher bzw.
History); die Frontends lesen daraus einen schmalen Balken.

**Prozent-Semantik (Entscheid F1a):** gewichtete Phasen, kein Zeitmodell. Jede
Phase traegt ein festes Gewicht (`WEIGHTS`), innerhalb der Phase zaehlt
`done/total`::

    pct = 100 * (Σ Gewichte der abgeschlossenen Phasen
                 + Gewicht der aktuellen * done/total)
          / Σ Gewichte aller Phasen im Plan DIESES Laufs

Der Plan ist also der Nenner: eine per `skip()` gestrichene Phase (z. B. keine
Rang-Schaetzung, weil niemand unranked ist) faellt aus der Summe und ihr Anteil
verteilt sich automatisch auf die uebrigen - ohne Sonderregel je Fall. Der Wert
sinkt innerhalb eines Laufs nie (`_floor`): ein springender Balken sieht nach
Fehler aus, selbst wenn nur eine Phase kuerzer ausfiel als gedacht.

**Unbestimmte Phasen** (`indeterminate=True`, z. B. "Warte auf Riot") haben
keinen sinnvollen Anteil - dort bleibt `pct` auf dem bis dahin erreichten Wert
stehen und das Frontend zeigt einen pulsenden statt eines gefuellten Balkens.

**Thread-Sicherheit:** der Worker schreibt, der HTTP-Handler liest. Jeder
Zugriff laeuft unter einem eigenen Lock, `snapshot()` liefert immer ein FRISCHES
Dict (nie das lebende Objekt). `on_change` wird bewusst NACH dem Freigeben des
Locks gerufen: die Senken haben eigene Locks (`_RETRY_LOCK`, `_report_lock`),
und ein Callback unter unserem Lock koennte sich mit ihnen verklemmen.

Alle Bau-Funktionen nehmen `progress=NOOP` als Default - der CLI-Pfad
(`postgame.run`) und jeder bestehende Aufruf verhalten sich damit unveraendert.
"""

import threading
from datetime import datetime

# Anteil jeder Phase am Gesamtfortschritt (F1a). Die Zahlen sind grobe
# Erfahrungswerte aus dem Ist-Zustand: die Raenge und vor allem die
# Peer-Schaetzung machen fast alle Riot-Calls und damit fast die ganze Wartezeit
# aus, Laden/Auswerten/Schreiben sind Sekundenbruchteile. "wait_riot" (Warten
# auf die Riot-Indexierung nach Spielende) traegt bewusst 0: wie lange Riot
# braucht, ist unbekannt - die Phase laeuft als unbestimmter Balken.
WEIGHTS = {
    "search": 10,       # Match-Suche (Resolver der Match-History)
    "wait_riot": 0,     # Warten auf die Indexierung (unbestimmt)
    "load": 10,         # Match + Timeline laden
    "analyze": 5,       # Serien/Analyse (rein lokal)
    "ranks": 35,        # Raenge der 10 Spieler
    "estimate": 40,     # Peer-Schaetzung der unranked Spieler
    "write": 10,        # Report schreiben
}


def _now() -> str:
    """Zeitstempel eines Fortschritts-Schritts (lokale Zone, ISO-8601)."""
    return datetime.now().astimezone().isoformat()


class Progress:
    """Fortschritt EINES Report-Laufs entlang eines festen Phasen-Plans.

    `plan` ist die erwartete Phasenfolge (Schluessel aus `WEIGHTS`), `on_change`
    ein optionaler Callback, der nach jeder Aenderung den frischen Snapshot
    bekommt. Ein Callback-Fehler wird geschluckt - die Anzeige darf einen
    Report-Bau nie kippen."""

    def __init__(self, plan, on_change=None):
        self._lock = threading.RLock()
        self._on_change = on_change
        # Reihenfolge des Plans zaehlt (sie bestimmt, was "vorher" ist);
        # Duplikate wuerden das Gewicht doppelt zaehlen.
        self._plan: list[str] = []
        for key in plan or ():
            if key not in self._plan:
                self._plan.append(key)
        self._key: str | None = None
        self._label = ""
        self._done = 0
        self._total = None
        self._indeterminate = False
        self._note = None
        self._floor = 0            # hoechster bisher gemeldeter Prozentwert
        self._finished = False
        self._updated_at = _now()

    # --- Schreibende Seite (Worker-Thread) ---------------------------------

    def phase(self, key, label, total=None, indeterminate=False) -> None:
        """Eine Phase betreten. Alle Plan-Phasen DAVOR gelten als abgeschlossen.

        `label` ist ein Format-String mit den optionalen Platzhaltern `{done}`
        und `{total}` ("Ränge {done}/{total}") - so bleibt der Text eine
        Vorlage und muss nicht bei jedem Tick neu gesetzt werden.

        Erneutes Betreten der AKTUELLEN Phase aktualisiert nur Label/`total`/
        `indeterminate` und setzt nichts zurueck (der Zaehler laeuft weiter) -
        genau das braucht "Warte auf Riot – Versuch X/Y", das je Versuch neu
        gesetzt wird. Ein Schluessel, der nicht im Plan steht, wird hinten
        angehaengt statt abgelehnt: eine falsche Phasen-Reihenfolge darf die
        Anzeige verstellen, aber nie den Bau abbrechen."""
        with self._lock:
            if key not in self._plan:
                self._plan.append(key)
            if key != self._key:
                self._key = key
                self._done = 0
                self._note = None
                self._finished = False
            self._label = label
            self._total = total
            self._indeterminate = bool(indeterminate)
            self._updated_at = _now()
            snap = self._snapshot_locked()
        self._emit(snap)

    def tick(self, n: int = 1, note=None) -> None:
        """Den Zaehler der laufenden Phase erhoehen (nie ueber `total` hinaus).

        `note` haengt einen Zusatz mit " · " ans Label ("Calls 58/210") - er
        bleibt bis zum naechsten Phasenwechsel stehen. Ohne betretene Phase ein
        striktes No-Op."""
        with self._lock:
            if self._key is None:
                return
            done = self._done + int(n)
            if self._total is not None:
                done = min(done, int(self._total))
            self._done = max(0, done)
            if note is not None:
                self._note = note
            self._updated_at = _now()
            snap = self._snapshot_locked()
        self._emit(snap)

    def skip(self, key) -> None:
        """Eine noch nicht betretene Phase aus dem Plan streichen.

        Damit faellt ihr Gewicht aus dem Nenner und ihr Anteil verteilt sich auf
        die uebrigen Phasen (Beispiel: kein unranked Spieler -> keine
        Schaetzung). Eine bereits betretene oder laufende Phase bleibt stehen -
        sonst koennte der Prozentwert nachtraeglich springen."""
        with self._lock:
            if key not in self._plan or key == self._key:
                return
            if (self._key is not None
                    and self._plan.index(key) < self._plan.index(self._key)):
                return   # schon durchlaufen - zaehlt als erledigt
            self._plan.remove(key)
            self._updated_at = _now()
            snap = self._snapshot_locked()
        self._emit(snap)

    def finish(self) -> None:
        """Den Lauf als fertig markieren (pct 100). Ohne betretene Phase ein
        No-Op - ein Lauf, der nie angefangen hat, ist auch nicht fertig."""
        with self._lock:
            if self._key is None:
                return
            self._finished = True
            if self._total is not None:
                self._done = int(self._total)
            self._updated_at = _now()
            snap = self._snapshot_locked()
        self._emit(snap)

    # --- Lesende Seite (HTTP-Handler) --------------------------------------

    def snapshot(self) -> dict | None:
        """Frisches Snapshot-Dict oder None, solange keine Phase betreten wurde.

        Form::

            {"phase": str, "label": str, "done": int, "total": int|None,
             "pct": int|None, "indeterminate": bool, "updated_at": iso-str}
        """
        with self._lock:
            return self._snapshot_locked()

    # --- Intern -------------------------------------------------------------

    def _snapshot_locked(self) -> dict | None:
        """Snapshot bauen - Aufrufer haelt den Lock (`_floor` wird fortgeschrieben)."""
        if self._key is None:
            return None
        total_w = sum(WEIGHTS.get(k, 0) for k in self._plan)
        idx = self._plan.index(self._key)
        before = sum(WEIGHTS.get(k, 0) for k in self._plan[:idx])
        if not total_w:
            # Degenerierter Plan (nur gewichtslose Phasen): ehrlich kein Prozent
            # statt einer Division durch 0.
            pct = None
        elif self._finished:
            pct = 100
        else:
            frac = 0.0
            if not self._indeterminate and self._total:
                frac = min(1.0, self._done / float(self._total))
            raw = 100.0 * (before + WEIGHTS.get(self._key, 0) * frac) / total_w
            pct = max(0, min(100, int(round(raw))))
        if pct is not None:
            # Monotonie: der Balken darf innerhalb eines Laufs nie zurueckfallen.
            pct = max(pct, self._floor)
            self._floor = pct
        return {"phase": self._key, "label": self._text(), "done": self._done,
                "total": self._total, "pct": pct,
                "indeterminate": bool(self._indeterminate),
                "updated_at": self._updated_at}

    def _text(self) -> str:
        """Fertiger Anzeigetext: Label-Vorlage gefuellt, `note` angehaengt.

        Ein unbekannter Platzhalter (Tippfehler im Label) faellt auf den rohen
        Text zurueck - eine Anzeige darf nie werfen. `total=None` erscheint als
        "?" statt als "None"."""
        total = self._total if self._total is not None else "?"
        try:
            label = self._label.format(done=self._done, total=total)
        except (IndexError, KeyError, ValueError):
            label = self._label
        return f"{label} · {self._note}" if self._note else label

    def _emit(self, snap) -> None:
        """Den Callback ausserhalb des Locks bedienen (s. Modul-Docstring)."""
        if snap is None or self._on_change is None:
            return
        try:
            self._on_change(snap)
        except Exception:   # noqa: BLE001 - die Anzeige darf nie den Bau kippen
            pass


class _NoProgress:
    """No-Op-Fortschritt: nimmt alles an, merkt sich nichts, liefert None.

    Default jeder `progress=`-Signatur, damit CLI- und Alt-Aufrufe ohne
    Fortschritt exakt so laufen wie vorher (kein `if progress is not None`
    quer durch den Bau-Code)."""

    def phase(self, key, label, total=None, indeterminate=False) -> None:
        pass

    def tick(self, n: int = 1, note=None) -> None:
        pass

    def skip(self, key) -> None:
        pass

    def finish(self) -> None:
        pass

    def snapshot(self) -> None:
        return None


NOOP = _NoProgress()
