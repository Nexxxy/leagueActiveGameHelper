"""Anbindung an die Live Client Data API (laeuft waehrend eines aktiven Spiels
lokal auf Port 2999, selbstsigniertes Zertifikat -> verify=False).
"""

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://127.0.0.1:2999/liveclientdata"


def fetch_allgamedata() -> dict | None:
    """Kompletter Spielzustand oder None, wenn kein Spiel laeuft."""
    try:
        resp = requests.get(f"{BASE}/allgamedata", timeout=2, verify=False)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data.get("allPlayers"):
        return None
    return data
