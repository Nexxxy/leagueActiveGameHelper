"""Minimaler Riot-API-Client mit Rate-Limiting für Personal Keys.

Personal-Key-Limits: 20 Requests/Sekunde und 100 Requests/2 Minuten.
Der Client drosselt selbst und respektiert zusätzlich Retry-After bei 429.
Unterstuetzt mehrere API-Keys im Round-Robin (eigener Rate-Limit-Bucket je Key).

Strg-C (`stop_event`): die Crawl-Kommandos haengen dem Client nachtraeglich ihr
`threading.Event` an (wie `client.log`). Ist es gesetzt, nutzt der Client die
noch freie Quota auf, wartet aber NIE auf ein neues Rate-Fenster: muesste der
naechste Request laenger als `STOP_MAX_WAIT_S` warten, wirft er `CrawlStopped`
statt zu schlafen. Ohne Event (App, Skripte) verhaelt sich alles wie zuvor.
"""

import sys
import time
from collections import deque

import requests


class CrawlStopped(Exception):
    """Stopp angefordert und der naechste Request muesste auf den
    Rate-Limiter warten."""


# Schwelle zwischen "Taktung" und "Fenster": Wartezeiten bis hierhin stammen vom
# Per-Sekunde-Limit (20/s) und werden auch nach Strg-C noch abgewartet - alles
# darueber ist das 2-Minuten-Fenster und beendet den Lauf (CrawlStopped).
# Bewusst unter `focus.WAIT_ABORT_S` (5 s): hier geht es um "warten oder nicht",
# dort um "lohnt sich ein neues Fenster".
STOP_MAX_WAIT_S = 1.0


def _default_log(msg: str) -> None:
    """Standard-Ausgabe fuer RiotClient-Meldungen: stderr (Verhalten wie
    frueher, als die Meldungen direkt mit file=sys.stderr gedruckt wurden)."""
    print(msg, file=sys.stderr)


# Abbruchmeldung, wenn kein aktiver Key mehr uebrig ist (drei Stellen in `_get`:
# kein Key beim Runden-Start, Key-Ablehnung per 401, Key-Ablehnung per 403).
_NO_KEYS = ("Alle API-Keys abgelehnt. Development-Keys laufen nach 24h ab "
            "- ggf. auf developer.riotgames.com erneuern.")

# Ab wie vielen VERSCHIEDENEN Endpunkten mit 403 ein Key als abgelaufen gilt.
# WARUM: ein einzelner 403 sagt nicht, ob der KEY oder der ENDPUNKT tot ist -
# real beobachtet antwortet der deprecated Endpunkt
# `league-v4/entries/by-summoner` mit 403 fuer voellig gueltige Keys. Ein
# abgelaufener Key dagegen antwortet auf ALLEN Endpunkten mit 403; sobald ein Key
# also am zweiten, anderen Endpunkt ebenfalls 403 liefert, ist der Key das
# Problem und fliegt aus der Rotation.
KEY_DEAD_403_ENDPOINTS = 2


class RiotClient:
    def __init__(self, api_key, platform: str, routing: str,
                 per_sec: int = 20, per_2min: int = 100, log=None):
        # `log` (optional, callable): Ausgabe-Wrapper fuer Key-Ablehnungen und
        # HTTP-Fehler. None -> Default `print` (Standalone, Verhalten
        # unveraendert). Im Multi-Region-Focus-Lauf wird er auf board.log
        # gesetzt, damit fremde prints den Statusblock nicht zerreissen. Als
        # Attribut nachtraeglich ueberschreibbar (client.log = board.log).
        self.log = log if log is not None else _default_log
        # `stop_event` (threading.Event | None): Strg-C-Signal des Laufs, von den
        # Crawl-Kommandos nachtraeglich gesetzt (client.stop_event = stop_event,
        # genau wie `log`). Gesetzt heisst: freie Quota noch aufbrauchen, aber
        # nie mehr auf ein Rate-Fenster warten (s. `_throttle`/`CrawlStopped`).
        # None -> Verhalten exakt wie ohne Stopp (App, Skripte, Tests).
        self.stop_event = None
        # Requests, die nach gesetztem `stop_event` noch rausgingen - die
        # Region-Worker nennen die Zahl in ihrer Abschlusszeile.
        self.requests_since_stop = 0
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
        # Endpunkt-Labels, die fuer diesen Lauf als tot gelten (403 ohne dass der
        # Key schuld ist, s. KEY_DEAD_403_ENDPOINTS). Weitere Aufrufe werden gar
        # nicht mehr abgesetzt - das spart Rate-Limit-Budget.
        self._dead_endpoints: set[str] = set()
        # Labels, deren Ueberspringen schon gemeldet wurde (eine Log-Zeile je
        # Endpunkt reicht; ein Crawl ruft denselben Endpunkt tausendfach).
        self._skip_logged: set[str] = set()
        # key_idx -> Endpunkt-Labels, die mit diesem Key 403 lieferten.
        self._key_403: dict[int, set[str]] = {}
        # Statuscode der ZULETZT erhaltenen HTTP-Antwort dieses Clients (None,
        # solange noch keine kam bzw. wenn `_get` gar keinen Request absetzte).
        # WOFUER: `_get` liefert 400 UND 404 gleichermassen als None. Wer beides
        # unterscheiden muss, liest den Code direkt nach dem Endpunkt-Aufruf -
        # eine key-FREMDE PUUID quittiert Riot mit 400 (PUUIDs sind pro API-Key
        # verschluesselt), eine unbekannte mit 404 (s.
        # `pipeline.harvest.match_ids_healed`).
        # Gilt je Client-Instanz und ist NICHT fuer nebenlaeufige Aufrufe
        # desselben Clients gedacht; im Projekt hat jede Region ihren eigenen
        # Client in ihrem eigenen Thread.
        self.last_status: int | None = None
        # Der Key, der zuletzt tatsaechlich GEANTWORTET hat (None, solange noch
        # keine Antwort kam).
        # WOFUER: Cache-Identitaet der Fairness-Sektion. PUUIDs sind an die
        # Key-FAMILIE gebunden (alle Dev-Keys eines Accounts teilen sie, der
        # Haupt-Key hat eigene) - der PUUID-Cache muss deshalb nach dem Key
        # gekeyt sein, der die Antwort geliefert hat, nicht nach dem beim
        # Anlegen konfigurierten. Der Unterschied ist real: `_FallbackClient`
        # (app/postgame/fetch.py) startet mit dem `dev_api_key` und schaltet
        # erst beim ERSTEN abgelehnten Call auf den `api_key` um - `_keys[0]`
        # ist bis dahin der ggf. laengst tote Dev-Key.
        self.last_key: str | None = None

    # ---- Round-Robin ---------------------------------------------------

    def _next_key(self) -> int | None:
        """Index des naechsten aktiven Keys per Round-Robin, oder None."""
        if not self._active:
            return None
        idx = self._active[self._rr % len(self._active)]
        self._rr += 1
        return idx

    def _stopping(self) -> bool:
        """True, wenn ein Stopp angefordert ist (Strg-C bzw. globaler Abbruch)."""
        return self.stop_event is not None and self.stop_event.is_set()

    def _pick_key(self) -> int | None:
        """Key fuer den naechsten Request; None, wenn keiner mehr aktiv ist.

        Regulaer Round-Robin (`_next_key`). NACH einem Stopp dagegen der aktive
        Key mit der GERINGSTEN Wartezeit: sonst koennte ein zufaellig voller
        Bucket den Lauf beenden, obwohl der zweite Key noch freie Quota hat -
        und genau die soll nach Strg-C ja noch aufgebraucht werden."""
        if not self._active or not self._stopping():
            return self._next_key()
        now = time.monotonic()
        return min(self._active, key=lambda i: self._wait_for_key(i, now))

    def _disable_key(self, key_idx: int) -> None:
        """Key dauerhaft aus der Rotation entfernen."""
        if key_idx in self._active:
            self._active.remove(key_idx)
            self.log(f"[riot] API-Key #{key_idx + 1} abgelehnt (401 bzw. 403 auf "
                     f"mehreren Endpunkten) - aus Round-Robin entfernt.")

    # ---- Rate-Limiting -------------------------------------------------

    def _wait_for_key(self, key_idx: int, now: float) -> float:
        """Wartezeit dieses Keys in Sekunden; 0.0 = sofort ein Slot frei.

        Read-only (raeumt den Bucket NICHT auf) - genau deshalb teilen sich
        `_throttle` (blockierend), `wait_seconds` (Vorschau) und `_pick_key`
        (Key-Wahl nach einem Stopp) dieselbe Rechnung."""
        bucket = self._sent[key_idx]
        waits = [0.0]
        for count, window in self.LIMITS:
            recent = [t for t in bucket if now - t <= window]
            if len(recent) >= count:
                waits.append(window - (now - recent[0]))
        return max(waits)

    def _throttle(self, key_idx: int) -> None:
        """Wartet, bis dieser Key wieder einen Slot frei hat.

        Nach einem Stopp (`stop_event` gesetzt) wird NIE auf ein neues
        Rate-Fenster gewartet: ist die Wartezeit groesser als `STOP_MAX_WAIT_S`,
        fliegt `CrawlStopped`. Kuerzere Wartezeiten (Per-Sekunde-Taktung) werden
        auch dann abgesessen - der Request geht noch raus.

        Ohne gesetztes Event, aber MIT Event-Objekt wird ueber `Event.wait`
        pausiert statt ueber `time.sleep`: Strg-C mitten in der 110-s-Pause weckt
        den Worker sofort, die Schleife rechnet neu und endet dann an derselben
        Regel. Ohne Event-Objekt bleibt es beim `time.sleep` wie zuvor."""
        bucket = self._sent[key_idx]
        while True:
            now = time.monotonic()
            while bucket and now - bucket[0] > 120:
                bucket.popleft()
            wait = self._wait_for_key(key_idx, now)
            if wait <= 0:
                return
            pause = wait + 0.05
            if self.stop_event is None:
                time.sleep(pause)
            elif self.stop_event.is_set():
                if wait > STOP_MAX_WAIT_S:
                    raise CrawlStopped(
                        f"Stopp angefordert, naechster Request muesste "
                        f"{wait:.0f}s auf das Rate-Fenster warten.")
                # Kurze Taktungs-Pause: hier hilft `Event.wait` nicht (es ist ja
                # schon gesetzt und kaeme sofort zurueck -> Leerlauf-Schleife).
                time.sleep(pause)
            else:
                # Rueckgabe True heisst "Event waehrend der Pause gesetzt" - die
                # naechste Runde wendet dieselbe Regel auf den Rest an.
                self.stop_event.wait(pause)

    def _retry_sleep(self, seconds: float) -> None:
        """Backoff-Pause in `_get` (429, 5xx, Verbindungsfehler).

        Nach einem Stopp gibt es keinen Retry mehr: ein 429 heisst ohnehin "im
        Limit", und ein 5xx-Retry nach einem gewollten Abbruch bringt nichts ->
        `CrawlStopped`. Dasselbe gilt, wenn das Event waehrend der Pause gesetzt
        wird. Ohne Event-Objekt: `time.sleep` wie zuvor."""
        if self.stop_event is None:
            time.sleep(seconds)
            return
        if self.stop_event.is_set() or self.stop_event.wait(seconds):
            raise CrawlStopped("Stopp angefordert - kein Retry mehr.")

    def wait_seconds(self) -> float:
        """Nicht-blockierende Vorschau: wie viele Sekunden muesste `_throttle`
        fuer den NAECHSTEN Request aktuell schlafen? 0.0, wenn sofort ein Slot
        frei ist. Aendert den Bucket NICHT und schlaeft nicht.

        Gleiche Fenster-Logik wie `_throttle` (beide LIMITS: per-Sekunde und
        per-2min), aber read-only. Bei mehreren aktiven Keys wird das MINIMUM
        ueber die aktiven Buckets geliefert: `_get` holt sich seinen Key ueber
        `_pick_key` und drosselt DIESEN Bucket - der guenstigste aktive Key
        bestimmt also, wie lange man realistisch spaetestens warten muss (nach
        einem Stopp waehlt `_pick_key` genau ihn). Ohne aktiven Key -> 0.0 (die
        Wartefrage ist dann ohnehin gegenstandslos; `_get` laeuft in den
        SystemExit fuer 'alle Keys weg')."""
        if not self._active:
            return 0.0
        now = time.monotonic()
        return min(self._wait_for_key(key_idx, now) for key_idx in self._active)

    def _get(self, host: str, path: str, params: dict | None = None,
             endpoint: str | None = None):
        """GET mit Rate-Limiting und Statuscode-Behandlung.

        `endpoint` ist ein STABILES Label des Endpunkts (z. B. "match-ids"),
        unabhaengig von den in `path` eingesetzten IDs - daran haengt die
        403-Behandlung. None -> der rohe `path` (dann ist jede ID ein eigener
        "Endpunkt", was fuer Adhoc-Aufrufe genuegt).

        200 -> JSON; 429 -> Retry-After abwarten; JEDER 5xx (>= 500) ->
        Backoff-Retry (nach 8 Versuchen RuntimeError); 404 -> None;
        401 -> Key deaktivieren; 403 -> endpunkt-lokal abschalten bzw. (ab
        KEY_DEAD_403_ENDPOINTS verschiedenen Endpunkten) Key deaktivieren;
        sonstige 4xx (400 etc.) -> Warnung auf stderr und None (ein kaputter
        Spieler soll den Lauf nicht abbrechen).

        WARUM 401 und 403 nicht mehr gleich behandelt werden: 403 heisst bei Riot
        nicht zwingend 'Key ungueltig'. Deprecated Endpunkte antworten mit 403
        fuer voellig gueltige Keys (real: `league-v4/entries/by-summoner`) - der
        Key global aus der Rotation zu werfen kostete dann den ganzen Lauf. Ein
        403 sperrt darum zunaechst nur DEN ENDPUNKT (`_dead_endpoints`, alle
        weiteren Aufrufe liefern ohne HTTP-Request None). Erst wenn DERSELBE Key
        auf mehreren verschiedenen Endpunkten 403 liefert, ist er wirklich
        abgelaufen und fliegt raus (s. KEY_DEAD_403_ENDPOINTS).

        `self.last_status` traegt nach jedem Aufruf den Statuscode der zuletzt
        erhaltenen HTTP-Antwort (None, wenn keine kam - abgeschalteter Endpunkt,
        reine Verbindungsfehler). Nur darueber laesst sich ein 400 von einem 404
        unterscheiden, weil beide als None zurueckkommen.

        `self.last_key` traegt dazu den Key, der diese Antwort geliefert hat -
        gesetzt bei jedem Status ausser 401/403 (nur die lehnen den KEY ab).
        Wer key-gebundene Daten cacht, muss nach diesem Key keyen, nicht nach
        dem konfigurierten (s. Attribut-Kommentar in `__init__`).

        Bewusst ALLE Statuscodes >= 500 (nicht nur 500/502/503/504) als
        transient behandeln: Riot laeuft hinter Cloudflare, das eigene
        5xx-Codes liefert (520/521/522/524). Die wurden frueher bis
        raise_for_status() durchgereicht und rissen den ganzen Fetch ab.

        Ist ein Stopp angefordert (`stop_event`), kann statt eines Ergebnisses
        `CrawlStopped` fliegen: sobald der naechste Request auf das Rate-Fenster
        warten muesste (s. `_throttle`) oder ein Retry anstuende (s.
        `_retry_sleep`). BEWUSST eine Ausnahme und kein None: None ist hier das
        Signal fuer 404, und die Fetch-Schleifen wuerden daraufhin einen
        `not_found`-Skip-Marker in den Cache schreiben."""
        self.last_status = None
        label = endpoint or path
        if label in self._dead_endpoints:
            if label not in self._skip_logged:
                self._skip_logged.add(label)
                self.log(f"[riot] Endpunkt {label} gilt fuer diesen Lauf als tot "
                         f"(403) - Aufrufe werden uebersprungen.")
            return None
        url = f"https://{host}{path}"
        for attempt in range(8):
            key_idx = self._pick_key()
            if key_idx is None:
                raise SystemExit(_NO_KEYS)
            self._throttle(key_idx)
            self._sent[key_idx].append(time.monotonic())
            if self._stopping():
                # Zaehlt, was nach dem Abbruch noch an freier Quota rausging -
                # die Region-Worker nennen die Zahl in ihrer Schluss-Zeile.
                self.requests_since_stop += 1
            try:
                resp = self._session.get(
                    url, params=params, timeout=15,
                    headers={"X-Riot-Token": self._keys[key_idx]},
                )
            except requests.RequestException:
                self._retry_sleep(3 * (attempt + 1))
                continue
            self.last_status = resp.status_code
            if resp.status_code not in (401, 403):
                # Alles ausser einer Key-Ablehnung heisst: DIESER Key hat
                # geantwortet (200 ebenso wie 429, 400/404 oder 5xx - die sagen
                # etwas ueber Anfrage bzw. Server, nicht ueber den Key). Damit
                # steht fest, welcher Key die Antwort erzeugt hat, s. `last_key`.
                self.last_key = self._keys[key_idx]
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", "10"))
                self._retry_sleep(retry + 1)
                continue
            if resp.status_code >= 500:
                # Alle 5xx (inkl. Cloudflare 520/521/522/524) sind transient.
                self._retry_sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 401:
                # 401 ist eindeutig: der Key selbst wird abgelehnt.
                self._disable_key(key_idx)
                if not self._active:
                    raise SystemExit(_NO_KEYS)
                continue
            if resp.status_code == 403:
                seen = self._key_403.setdefault(key_idx, set())
                seen.add(label)
                if len(seen) >= KEY_DEAD_403_ENDPOINTS:
                    # Muster 'abgelaufener Key': 403 auf mehreren Endpunkten.
                    self._disable_key(key_idx)
                    if not self._active:
                        raise SystemExit(_NO_KEYS)
                    continue
                self._dead_endpoints.add(label)
                self._skip_logged.add(label)
                self.log(f"[riot] Endpunkt {label} antwortet 403 - fuer diesen "
                         f"Lauf uebersprungen, Key bleibt aktiv.")
                return None
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
    #
    # Jede Methode gibt `_get` ein stabiles Endpunkt-Label mit (`endpoint=`):
    # der `path` traegt IDs/PUUIDs und ist damit je Aufruf anders - die
    # 403-Behandlung (s. `_get`) braucht aber einen Namen fuer DEN ENDPUNKT.

    def league(self, tier: str, queue: str = "RANKED_SOLO_5x5"):
        """tier: challenger | grandmaster | master"""
        return self._get(self.platform_host,
                         f"/lol/league/v4/{tier}leagues/by-queue/{queue}",
                         endpoint="league")

    def league_entries(self, tier: str, division: str, page: int = 1,
                       queue: str = "RANKED_SOLO_5x5"):
        """Fuer Tiers unterhalb Master: z.B. tier='DIAMOND', division='I'."""
        return self._get(
            self.platform_host,
            f"/lol/league/v4/entries/{queue}/{tier}/{division}",
            params={"page": page},
            endpoint="league-entries",
        )

    def league_entries_by_puuid(self, puuid: str):
        """Ranked-Eintraege EINES Spielers (Solo/Flex/...) ueber league-v4.

        Rueckgabe: rohe Entry-Liste (leere Liste = unranked), None bei 404 oder
        wenn Riot den Aufruf ablehnt (z. B. HTTP 400 fuer eine PUUID, die mit
        einem ANDEREN API-Key verschluesselt wurde - PUUIDs sind key-gebunden,
        s. `app/postgame/fairness.py`).

        Bewusst KEIN Fallback auf den deprecated Weg
        `/lol/league/v4/entries/by-summoner/{summonerId}`: der antwortet
        inzwischen mit 403 und liefert damit nichts als eine Warnzeile plus einen
        fuer den Rest des Laufs abgeschalteten Endpunkt (s. `_get`) - der tote
        Pfad waere also nutzlos."""
        return self._get(self.platform_host,
                         f"/lol/league/v4/entries/by-puuid/{puuid}",
                         endpoint="league-entries-by-puuid")

    def mastery_top(self, puuid: str, count: int = 15):
        """Top-Champion-Masteries eines Spielers (inkl. lastPlayTime)."""
        return self._get(
            self.platform_host,
            f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top",
            params={"count": count},
            endpoint="mastery-top",
        )

    def summoner_by_id(self, summoner_id: str):
        return self._get(self.platform_host,
                         f"/lol/summoner/v4/summoners/{summoner_id}",
                         endpoint="summoner")

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
            endpoint="match-ids",
        )

    def account_by_riot_id(self, game_name: str, tag_line: str):
        """Riot-ID (Name#Tag) -> Account (u. a. puuid) ueber account-v1.

        account-v1 laeuft auf dem Regional-Routing-Host (americas/asia/europe),
        nicht auf dem Platform-Host. Rueckgabe None bei 404 (unbekannte Riot-ID).
        """
        return self._get(
            self.routing_host,
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
            endpoint="account-by-riot-id",
        )

    def account_by_puuid(self, puuid: str):
        """PUUID -> Account (u. a. `gameName`/`tagLine`) ueber account-v1.

        Gegenrichtung zu `account_by_riot_id`, ebenfalls auf dem Regional-
        Routing-Host. Rueckgabe roh (Dict) bzw. None.

        **Key-Bindung:** Riot verschluesselt PUUIDs PRO API-KEY. Der Aufruf
        funktioniert also nur fuer PUUIDs, die mit DEMSELBEN Key aufgeloest
        wurden; eine fremde PUUID quittiert Riot mit HTTP 400, was `_get` bereits
        als None abfaengt (Warnzeile, kein Abbruch). Genutzt wird er, um von einer
        PUUID auf die key-UNABHAENGIGE Riot-ID zu kommen - der einzige
        Identitaetsvergleich, der auch gegen gecachte Matches eines anderen Keys
        trifft (s. `app/postgame/fetch.resolve_me_pid`)."""
        return self._get(
            self.routing_host,
            f"/riot/account/v1/accounts/by-puuid/{puuid}",
            endpoint="account-by-puuid",
        )

    def match(self, match_id: str):
        return self._get(self.routing_host,
                         f"/lol/match/v5/matches/{match_id}",
                         endpoint="match")

    def match_timeline(self, match_id: str):
        return self._get(self.routing_host,
                         f"/lol/match/v5/matches/{match_id}/timeline",
                         endpoint="timeline")
