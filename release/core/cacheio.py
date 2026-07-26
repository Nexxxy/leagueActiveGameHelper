"""Gemeinsame Cache-I/O-Schicht fuer die Pipeline.

Buendelt das JSON-Lesen/Schreiben, den billigen Skip-Marker-Peek, die
mtime-Frische-Pruefung und den Match-Cache-Scan an EINER Stelle. Diese Helfer
waren zuvor als unterstrich-private Funktionen in `harvest.py` (`_read_json`,
`_write_json`, `_peek`) und `focus.py` (`_fresh`) verstreut und wurden quer
durch das Package importiert bzw. mehrfach inline nachgebaut. Struktur-Review
2026-07-17, Befunde S4 (private Helfer als De-facto-API), D1 (Skip-Peek
dreifach dupliziert) und D2 (Match-Scan-Schleife fuenffach).

Die aufrufenden Module (`harvest`, `focus`, `pool`, `carryover`) importieren
die Helfer direkt von hier.
"""

import json
import time
from pathlib import Path

DEFAULT_TTL_S = 86_400   # 1 Tag: Default-Frische-Fenster (Screening-/Mastery-/
                         # Leiter-Cache); entspricht focus.SCREEN_TTL_S.


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def peek(path: Path) -> str:
    """Liest nur den Anfang der Datei, um Skip-Marker billig zu erkennen."""
    with path.open("r", encoding="utf-8") as fh:
        return fh.read(16)


def fresh(path, now: float | None = None, ttl: float = DEFAULT_TTL_S) -> bool:
    """True, wenn die Datei existiert und juenger als `ttl` ist (mtime).
    Default-TTL ist DEFAULT_TTL_S (Screening-/Mastery-/Leiter-Cache)."""
    if not path.exists():
        return False
    now = time.time() if now is None else now
    return (now - path.stat().st_mtime) < ttl
