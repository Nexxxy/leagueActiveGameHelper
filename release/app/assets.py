"""Optionaler Item-Icon-Downloader (Data Dragon).

Laedt die Item-Icons offiziell von Data Dragon (Dateiname = numerische
Item-ID) nach frontend/assets/items/. Bewusst KEIN HTML-Scraping vom Wiki -
Data Dragon liefert die Bilder direkt und stabil ueber die Item-ID.

Manuell nutzbar via  ``python -m app.assets``.
"""

import requests

from pipeline.config import ROOT
from pipeline.ddragon import BASE
from . import items

# frontend/assets/items/<id>.png - statisch vom Server gemountet.
ASSETS_DIR = ROOT / "frontend" / "assets" / "items"


def assets_available() -> bool:
    """True, wenn der Icon-Ordner existiert und mindestens ein .png enthaelt.

    Absichtlich NICHT gecacht: der Nutzer kann den Ordner waehrend der
    Serverlaufzeit anlegen/loeschen. Ein Verzeichnis-Scan pro /api/state-Aufruf
    ist billig genug.
    """
    if not ASSETS_DIR.is_dir():
        return False
    return any(ASSETS_DIR.glob("*.png"))


def download_missing(verbose: bool = True) -> int:
    """Laedt fehlende Item-Icons von Data Dragon nach ASSETS_DIR.

    Nur nicht vorhandene Dateien werden geholt (Nachladen). Einzelne
    Fehlschlaege (404, Timeout) werden uebersprungen. Rueckgabe: Anzahl neu
    geschriebener Dateien.
    """
    version, data = items._load()
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    failed = 0
    for item_id in data:
        target = ASSETS_DIR / f"{item_id}.png"
        if target.exists():
            skipped += 1
            continue
        url = f"{BASE}/cdn/{version}/img/item/{item_id}.png"
        try:
            resp = requests.get(url, timeout=15)
        except requests.RequestException:
            failed += 1
            continue
        if resp.status_code != 200:
            failed += 1
            continue
        # Erst bei HTTP 200 schreiben -> nie halbe/leere Dateien im Ordner.
        target.write_bytes(resp.content)
        written += 1
        if verbose and written % 25 == 0:
            print(f"  ... {written} Icons geladen")

    if verbose:
        print(f"Item-Icons: {written} neu geladen, {skipped} vorhanden, "
              f"{failed} uebersprungen -> {ASSETS_DIR}")
    return written


def main() -> None:
    download_missing()


if __name__ == "__main__":
    main()
