"""Minimaler Riot-API-Client mit Rate-Limiting für Personal Keys.

Personal-Key-Limits: 20 Requests/Sekunde und 100 Requests/2 Minuten.
Der Client drosselt selbst und respektiert zusätzlich Retry-After bei 429.
Unterstuetzt mehrere API-Keys im Round-Robin (eigener Rate-Limit-Bucket je Key).
"""

import sys
import time
from collections import deque

import requests


def _default_log(msg: str) -> None:
    """Standard-Ausgabe fuer RiotClient-Meldungen: stderr (Verhalten wie
    frueher, als die Meldungen direkt mit file=sys.stderr gedruckt wurden)."""
    print(msg, file=sys.stderr)


class RiotClient:
    def __init__(self, api_key, platform: str, routing: str,
                 per_sec: int = 20, per_2min: int = 100, log=None):
        # `log` (optional, callable): Ausgabe-Wrapper fuer Key-Ablehnungen und
        # HTTP-Fehler. None -> Default `print` (Standalone, Verhalten
        # unveraendert). Im Multi-Region-Focus-Lauf wird er auf board.log
        # gesetzt, damit fremde prints den Statusblock nicht zerreissen. Als
        # Attribut nachtraeglich ueberschreibbar (client.log = board.log).
        self.log = log if log is not None else _default_log
        # api_key: einzelner String ODER Liste/Tuple von Strings.
        if isinstance(api_key, str):
            raw = [api_key]
        else:
            raw = list(api_key)
        # Leere Strings raus, Duplikate entfernen (Reihenfolge erhalten).
        seen: set[str] = set()
        keys: list[str] = []
        for k in raw:
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        if not keys:
            raise SystemExit(
                "Kein API-Key gefunden. In config.yml unter riot.api_key eintragen "
                "oder RIOT_API_KEY als Umgebungsvariable setzen."
            )
        self._keys = keys
        self._active: list[int] = list(range(len(keys)))
        self._rr = 0
        self.LIMITS = ((per_sec, 1.0), (per_2min, 120.0))
        self.platform = platform    # z. B. "euw1"
        self.routing = routing      # z. B. "europe"
        self.platform_host = f"{platform}.api.riotgames.com"
        self.routing_host = f"{routing}.api.riotgames.com"
        self._session = requests.Session()
        self._sent: dict[int, deque] = {i: deque() for i in range(len(keys))}

    # ---- Round-Robin ---------------------------------------------------

    def _next_key(self) -> int | None:
        """Index des naechsten aktiven Keys per Round-Robin, oder None."""
        if not self._active:
            return None
        idx = self._active[self._rr % len(self._active)]
        self._rr += 1
        return idx

    def _disable_key(self, key_idx: int) -> None:
        """Key dauerhaft aus der Rotation entfernen."""
        if key_idx in self._active:
            self._active.remove(key_idx)
            self.log(f"[riot] API-Key #{key_idx + 1} abgelehnt (401/403) "
                     f"- aus Round-Robin entfernt.")

    # ---- Rate-Limiting -------------------------------------------------

    def _throttle(self, key_idx: int) -> None:
        bucket = self._sent[key_idx]
        while True:
            now = time.monotonic()
            while bucket and now - bucket[0] > 120:
                bucket.popleft()
            waits = []
            for count, window in self.LIMITS:
                recent = [t for t in bucket if now - t <= window]
                if len(recent) >= count:
                    waits.append(window - (now - recent[0]))
            if not waits:
                return
            time.sleep(max(waits) + 0.05)

    def wait_seconds(self) -> float:
        """Nicht-blockierende Vorschau: wie viele Sekunden muesste `_throttle`
        fuer den NAECHSTEN Request aktuell schlafen? 0.0, wenn sofort ein Slot
        frei ist. Aendert den Bucket NICHT und schlaeft nicht.

        Gleiche Fenster-Logik wie `_throttle` (beide LIMITS: per-Sekunde und
        per-2min), aber read-only. Bei mehreren aktiven Keys wird das MINIMUM
        ueber die aktiven Buckets geliefert: `_get` holt sich den naechsten Key
        per Round-Robin (`_next_key`) und drosselt DIESEN Bucket - der guenstigste
        aktive Key bestimmt also, wie lange man realistisch spaetestens warten
        muss. Ohne aktiven Key -> 0.0 (die Wartefrage ist dann ohnehin
        gegenstandslos; `_get` laeuft in den SystemExit fuer 'alle Keys weg')."""
        if not self._active:
            return 0.0
        now = time.monotonic()
        best = None
        for key_idx in self._active:
            bucket = self._sent[key_idx]
            waits = [0.0]
            for count, window in self.LIMITS:
                recent = [t for t in bucket if now - t <= window]
                if len(recent) >= count:
                    waits.append(window - (now - recent[0]))
            wait = max(waits)
            if best is None or wait < best:
                best = wait
        return best if best is not None else 0.0

    def _get(self, host: str, path: str, params: dict | None = None):
        """GET mit Rate-Limiting und Statuscode-Behandlung.

        200 -> JSON; 429 -> Retry-After abwarten; JEDER 5xx (>= 500) ->
        Backoff-Retry (nach 8 Versuchen RuntimeError); 404 -> None;
        401/403 -> Key deaktivieren; sonstige 4xx (400 etc.) -> Warnung
        auf stderr und None (ein kaputter Spieler soll den Lauf nicht
        abbrechen).

        Bewusst ALLE Statuscodes >= 500 (nicht nur 500/502/503/504) als
        transient behandeln: Riot laeuft hinter Cloudflare, das eigene
        5xx-Codes liefert (520/521/522/524). Die wurden frueher bis
        raise_for_status() durchgereicht und rissen den ganzen Fetch ab."""
        url = f"https://{host}{path}"
        for attempt in range(8):
            key_idx = self._next_key()
            if key_idx is None:
                raise SystemExit(
                    "Alle API-Keys abgelehnt. Development-Keys laufen nach 24h "
                    "ab - ggf. auf developer.riotgames.com erneuern."
                )
            self._throttle(key_idx)
            self._sent[key_idx].append(time.monotonic())
            try:
                resp = self._session.get(
                    url, params=params, timeout=15,
                    headers={"X-Riot-Token": self._keys[key_idx]},
                )
            except requests.RequestException:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", "10"))
                time.sleep(retry + 1)
                continue
            if resp.status_code >= 500:
                # Alle 5xx (inkl. Cloudflare 520/521/522/524) sind transient.
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403):
                self._disable_key(key_idx)
                if not self._active:
                    raise SystemExit(
                        "Alle API-Keys abgelehnt. Development-Keys laufen nach "
                        "24h ab - ggf. auf developer.riotgames.com erneuern."
                    )
                continue
            # Verbleibende 4xx (z. B. 400 fuer PUUIDs, die der Endpoint nicht
            # verarbeiten kann - etwa Altbestand aus Caches): EIN kaputter
            # Spieler darf den Lauf nicht killen. Warnen und ueberspringen.
            # Path ohne Query-Params/Key loggen (keine Secrets in stderr).
            if 400 <= resp.status_code < 500:
                self.log(f"[riot] HTTP {resp.status_code} fuer {path} "
                         f"- uebersprungen")
                return None
            resp.raise_for_status()
        raise RuntimeError(f"Zu viele Fehlversuche: {url}")

    # ---- Endpoints -----------------------------------------------------

    def league(self, tier: str, queue: str = "RANKED_SOLO_5x5"):
        """tier: challenger | grandmaster | master"""
        return self._get(self.platform_host, f"/lol/league/v4/{tier}leagues/by-queue/{queue}")

    def league_entries(self, tier: str, division: str, page: int = 1,
                       queue: str = "RANKED_SOLO_5x5"):
        """Fuer Tiers unterhalb Master: z.B. tier='DIAMOND', division='I'."""
        return self._get(
            self.platform_host,
            f"/lol/league/v4/entries/{queue}/{tier}/{division}",
            params={"page": page},
        )

    def mastery_top(self, puuid: str, count: int = 15):
        """Top-Champion-Masteries eines Spielers (inkl. lastPlayTime)."""
        return self._get(
            self.platform_host,
            f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top",
            params={"count": count},
        )

    def summoner_by_id(self, summoner_id: str):
        return self._get(self.platform_host, f"/lol/summoner/v4/summoners/{summoner_id}")

    def match_ids(self, puuid: str, queue: int | None = None, count: int = 20,
                  start_time: int | None = None, *,
                  end_time: int | None = None,
                  type_filter: str | None = "ranked"):
        """Match-IDs eines Spielers. `start_time` (Epoch-SEKUNDEN, optional)
        wird als startTime an die Match-v5-API durchgereicht - dann liefert
        Riot nativ nur Matches ab diesem Zeitpunkt (Patch-Grenze).

        `end_time` (keyword-only, Epoch-SEKUNDEN, optional) ist das Gegenstueck
        und wird als endTime durchgereicht. Zusammen mit `start_time` ergibt das
        ein ZEITFENSTER - der History-Retry holt so nur die Spiele des fraglichen
        Abends statt der letzten N Spiele ueber alle Queues (spart Quota und
        findet auch mehrere Tage alte Reports).

        `queue` (optional): auf eine einzelne Queue-ID einschraenken; None ->
        kein Queue-Filter (alle Queues). `type_filter` (keyword-only, Default
        'ranked'): Match-Typ-Filter der Match-v5-API; None laesst ihn weg, dann
        kommen auch Normal-Spiele. Die Crawler (focus/harvest) rufen mit
        queue=cfg.queue und type='ranked' auf (Verhalten unveraendert); der
        Post-Game-`--latest`-Pfad braucht queue=None + type_filter=None, um das
        NEUESTE Spiel unabhaengig von ranked/normal zu finden."""
        params: dict = {"count": count}
        if queue is not None:
            params["queue"] = queue
        if type_filter is not None:
            params["type"] = type_filter
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return self._get(
            self.routing_host,
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            params=params,
        )

    def account_by_riot_id(self, game_name: str, tag_line: str):
        """Riot-ID (Name#Tag) -> Account (u. a. puuid) ueber account-v1.

        account-v1 laeuft auf dem Regional-Routing-Host (americas/asia/europe),
        nicht auf dem Platform-Host. Rueckgabe None bei 404 (unbekannte Riot-ID).
        """
        return self._get(
            self.routing_host,
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
        )

    def match(self, match_id: str):
        return self._get(self.routing_host, f"/lol/match/v5/matches/{match_id}")

    def match_timeline(self, match_id: str):
        return self._get(self.routing_host, f"/lol/match/v5/matches/{match_id}/timeline")
