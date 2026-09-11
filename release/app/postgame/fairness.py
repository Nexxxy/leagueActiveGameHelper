"""Fairness-Sektion des Post-Game-Reports: war die Lobby ausgeglichen?

Drei Bausteine, die EINZELN degradieren - faellt einer aus (kein Key, API tot,
keine Peers), fehlt nur sein Beitrag, nie die ganze Sektion:

1. **Rollen-Wahl + Account-Level** - offline aus dem Match (`role_choice`),
   kostenlos, immer da. Beantwortet "Erstwahl / Zweitwahl / Autofill".
2. **Rang** - Riot-ID -> account-v1 -> PUUID -> league-v4, Cache-first,
   Fallback Solo/Duo -> Flex (`RankLookup`, `ranks_for`).
3. **Peer-Schaetzung** - nur fuer Spieler ohne jeden Ranked-Eintrag, aus den
   Mitspielern ihrer letzten SR-5v5-Spiele (Tiefe aus `postgame.estimate_matches`),
   budgetiert und sichtbar als Schaetzung gekennzeichnet (`estimate_rank`).

**Identitaets-Regel (bindend, aus dem Spike am realen Spiel):** PUUIDs sind
**pro Key-FAMILIE verschluesselt**. Dieselbe Person traegt unter dem
`dev_api_key` eine andere PUUID als unter dem `api_key` - und der Roh-Cache der
Pipeline enthaelt die PUUIDs desjenigen Keys, der das Match geholt hat. Eine
PUUID aus einem GECACHTEN Match gegen account-v1/league-v4 zu halten endet darum
in HTTP 400. Jeder Weg nach draussen startet deshalb bei der **Riot-ID**
(`riotIdGameName#riotIdTagline`, key-unabhaengig) und loest sie mit DEMSELBEN
Client auf, der danach fragt; der PUUID-Cache ist entsprechend nach
(Key-Identitaet, Riot-ID) gekeyt. Einzige Ausnahme: ein Match, das wir in
diesem Lauf SELBST geholt haben - dessen PUUIDs gehoeren unserem Key und sind
direkt verwendbar (halbiert die Kosten der Peer-Schaetzung).

Drei Praezisierungen, die ein realer Fall (abgelaufener Dev-Key) erzwungen hat:

- **Familie, nicht String.** Am Cache beobachtet: an drei Tagen ERNEUERTE
  Dev-Keys liefern identische PUUIDs, der Haupt-Key voellig andere. Die
  Verschluesselung haengt also an der Key-Familie (Dev-App vs. Haupt-Key). Der
  Fingerabdruck ueber den Key-String bleibt trotzdem die Identitaet: er trennt
  garantiert richtig; ein erneuerter Dev-Key kostet nur einen Cache-Miss.
- **Der Key, der GEANTWORTET hat.** Identitaet ist `client.last_key`, nicht der
  beim Anlegen konfigurierte Key. `fetch._FallbackClient` startet mit dem
  `dev_api_key` und schaltet erst beim ersten abgelehnten Call auf den `api_key`
  um - eine beim Anlegen eingefrorene Identitaet schriebe die Antworten des
  Haupt-Keys unter die Dev-Kennung (und laese die Dev-Altbestaende als
  vermeintlich passend wieder hoch).
- **Key-Wechsel mitten im Abruf.** Faellt der Proxy waehrend eines Lookups um,
  gehoert die eben benutzte PUUID noch zum alten Key und Riot quittiert sie mit
  HTTP 400. `rank_by_riot_id` loest die Riot-ID dann genau EINMAL unter der
  neuen Identitaet neu auf.

Aus derselben Regel folgt: Dedup und Lobby-Ausschluss der Peer-Auswahl laufen
ueber die volle Riot-ID (case-insensitiv), NIE ueber die PUUID - dieselbe
Person haette sonst je nach Herkunft des Matches zwei verschiedene PUUIDs und
der Ausschluss griffe nicht.

Kein Fehlerpfad wirft: jeder Ausfall endet in "Teil fehlt".
"""

import hashlib
import statistics

from core import ddragon, shardstore

from . import fetch
# Direkt-Import: der Parameter heisst selbst `progress` und wuerde das Modul
# im Funktionskoerper verdecken.
from .progress import NOOP

# --- Rang-Skala -------------------------------------------------------------
# score = tier_index * 400 + division_index * 100 + LP. Master+ traegt keine
# Division (Riot liefert dort immer "I"), die LP laufen linear weiter - im
# Zielsegment des Tools (Emerald/Diamond abwaerts) spielt das ohnehin keine
# Rolle, und jede Deckelung waere eine erfundene Konstante.

TIER_ORDER = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
              "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER")
TIER_INDEX = {t: i for i, t in enumerate(TIER_ORDER)}
APEX_FROM = TIER_INDEX["MASTER"]          # ab hier ohne Division
DIVISION_INDEX = {"IV": 0, "III": 1, "II": 2, "I": 3}
DIVISION_ROMAN = {0: "IV", 1: "III", 2: "II", 3: "I"}
TIER_POINTS = 400
DIVISION_POINTS = 100

# Queue-Typ der league-v4-Entries -> interne Quelle (Fallback-Reihenfolge).
QUEUE_SOURCE = {"RANKED_SOLO_5x5": "solo", "RANKED_FLEX_SR": "flex"}
SOURCE_ORDER = ("solo", "flex")

# --- Rollen-Wahl ------------------------------------------------------------
# `selectedRolePreferences`: '<zugewiesen>.<wie>.<wahl1>.<wahl2>[.FILL]',
# z. B. 'TOP.PRIMARY.TOP.MIDDLE' oder 'UTILITY.FILL_PRIMARY.FILL.UNSELECTED.FILL'.
HOW_TOKENS = frozenset({"PRIMARY", "SECONDARY", "AUTOFILL",
                        "FILL_PRIMARY", "FILL_SECONDARY"})
PLACEHOLDER_PICKS = frozenset({"UNSELECTED", "FILL", ""})
ROLES = frozenset({"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"})

# Badge-Wording (Nutzer-Entscheid): SECONDARY = auf der Zweitwahl gelandet,
# AUTOFILL/FILL_* = Rolle zugeteilt. PRIMARY ist ein Nicht-Befund -> kein Badge.
CHOICE_BADGE = {"SECONDARY": "OFFROLE", "AUTOFILL": "AUTOFILL",
                "FILL_PRIMARY": "AUTOFILL", "FILL_SECONDARY": "AUTOFILL"}

# --- Remake -----------------------------------------------------------------
# Ein Remake sagt ueber die Zusammenstellung nichts aus (niemand hat gespielt) -
# die Sektion entfaellt komplett, VOR jedem Call.
REMAKE_MAX_SECONDS = 300

# --- Peer-Schaetzung --------------------------------------------------------
# Tiefe (Default, ueberschreibbar per `postgame.estimate_matches`): mehr SPIELE
# statt mehr Peers je Spiel. Die Mitspieler DERSELBEN Lobby hat das Matchmaking
# auf nahezu denselben Rang zusammengestellt - sie sind stark korreliert, drei
# Peers aus einem Spiel tragen also kaum mehr Information als einer. Unabhaengige
# Beobachtungen entstehen erst ueber verschiedene Lobbys, darum wuchs die Zahl
# der Referenzspiele (2 -> 5) und nicht `PEER_PER_MATCH`.
PEER_MATCHES = 5            # so viele Referenzspiele je unranked Spieler
PEER_PER_MATCH = 3          # so viele Mitspieler je Referenzspiel
# Match-IDs werden mit Reserve angefordert: ob ein Spiel SR-5v5 ist, steht erst
# NACH dem Match-Fetch fest (ARAM/Arena fallen dort raus). Ohne Reserve landete
# jeder, der zwischendurch ARAM spielt, unter der gewuenschten Tiefe. Der
# groessere `count` kostet KEINEN zusaetzlichen Call (derselbe match-ids-Aufruf),
# und der Faktor deckelt den Mehraufwand hart auf PEER_MATCHES * 3 Match-Fetches.
PEER_SCAN_FACTOR = 3
# Abgeleitetes Call-Budget je Referenzspiel-Tiefe. Gemessen kosteten 6 unranked
# Spieler bei Tiefe 2 rund 51 Calls - 30 je Tiefenstufe reproduziert bei Tiefe 2
# exakt den frueheren Fixwert 60 und skaliert mit.
BUDGET_PER_MATCH = 30
RANKED_QUEUES = frozenset({420, 440})

# TTL des Riot-ID -> PUUID-Caches. Die Abbildung ist stabil, bis jemand seine
# Riot-ID aendert - dann laeuft der Eintrag nach spaetestens einer Woche aus.
ACCOUNT_TTL_S = 7 * 24 * 3600

# Verdikt-Schwellen in DIVISIONEN (|Delta| des Team-Mittels / 100).
EVEN_MAX_DIV = 0.5
SLIGHT_MAX_DIV = 2.0


# ============================================================================
# 1. Rang-Skala (reine Rechnung, offline)
# ============================================================================

def tier_label(tier: str, division=None) -> str:
    """('PLATINUM', 'I') -> 'Platinum I'; Master+ ohne Division."""
    name = str(tier or "").strip().upper()
    idx = TIER_INDEX.get(name)
    if idx is None:
        return ""
    if idx >= APEX_FROM or not division:
        return name.title()
    return f"{name.title()} {str(division).strip().upper()}"


def entry_score(entry: dict):
    """league-v4-Entry -> Punkte (tier*400 + division*100 + LP) oder None."""
    if not isinstance(entry, dict):
        return None
    idx = TIER_INDEX.get(str(entry.get("tier") or "").strip().upper())
    if idx is None:
        return None
    div = 0
    if idx < APEX_FROM:
        div = DIVISION_INDEX.get(str(entry.get("rank") or "").strip().upper(), 0)
    try:
        lp = int(entry.get("leaguePoints") or 0)
    except (TypeError, ValueError):
        lp = 0
    return idx * TIER_POINTS + div * DIVISION_POINTS + lp


def score_label(score) -> str:
    """Punkte -> Tier-Label ('Emerald IV'). Master+ ohne Division (s. F5).

    Nur fuer ABGELEITETE Werte gedacht (Team-Mittel, Peer-Schaetzung); der Rang
    eines einzelnen Spielers traegt sein eigenes Label aus dem Entry. Oberhalb
    von Master laufen die LP linear weiter und koennen einen Wert rechnerisch
    ins naechste Apex-Tier heben - das ist gewollt: Master/GM/Challenger sind in
    Riots Leiter selbst nur LP-Schwellen, keine eigenen Stufen."""
    if score is None:
        return ""
    value = max(0, int(round(score)))
    idx = min(len(TIER_ORDER) - 1, value // TIER_POINTS)
    if idx >= APEX_FROM:
        return TIER_ORDER[idx].title()
    rest = value - idx * TIER_POINTS
    div = max(0, min(3, rest // DIVISION_POINTS))
    return f"{TIER_ORDER[idx].title()} {DIVISION_ROMAN[div]}"


def rank_from_entries(entries):
    """Entry-Liste -> Rang-Dict (Solo/Duo vor Flex) oder None.

    None heisst 'kein Ranked-Eintrag' - also unranked ODER Abruf fehlgeschlagen;
    beide Faelle enden fuer die Sektion gleich (Rang-Teil fehlt)."""
    if not entries:
        return None
    best: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = QUEUE_SOURCE.get(str(entry.get("queueType") or "").strip())
        score = entry_score(entry) if source else None
        if source and score is not None and source not in best:
            best[source] = (entry, score)
    for source in SOURCE_ORDER:
        found = best.get(source)
        if not found:
            continue
        entry, score = found
        tier = str(entry.get("tier") or "").strip().upper()
        division = str(entry.get("rank") or "").strip().upper()
        return {
            "tier": tier,
            "division": division if TIER_INDEX.get(tier, 0) < APEX_FROM else None,
            "lp": int(entry.get("leaguePoints") or 0),
            "queue": entry.get("queueType"),
            "score": score,
            "source": source,
            "label": tier_label(tier, division),
            "estimated": False,
        }
    return None


# ============================================================================
# 2. Offline-Bausteine aus dem Match
# ============================================================================

def riot_id_of(part: dict) -> str:
    """'Name#Tag' eines Participants; leer, wenn Riot die Felder nicht liefert.

    Der `summonerName` ist bewusst KEIN Fallback: er ist seit der Riot-ID-
    Umstellung leer bzw. veraltet und taugt weder als Identitaet fuer
    account-v1 noch als Dedup-Schluessel."""
    name = str((part or {}).get("riotIdGameName") or "").strip()
    tag = str((part or {}).get("riotIdTagline") or "").strip()
    return f"{name}#{tag}" if name and tag else ""


def role_choice(part: dict) -> dict:
    """`selectedRolePreferences` parsen - rein offline, kein Netz.

    Rueckgabe `{"assigned", "how", "picks"}`. `how` ist eines der Tokens
    PRIMARY/SECONDARY/AUTOFILL/FILL_PRIMARY/FILL_SECONDARY, sonst None
    (fehlendes oder unbekanntes Feld -> kein Badge, kein Crash). `picks` sind
    die tatsaechlich gewaehlten Rollen ohne die Platzhalter UNSELECTED/FILL."""
    raw = str((part or {}).get("selectedRolePreferences") or "").strip()
    tokens = [t.strip().upper() for t in raw.split(".") if t.strip()]
    assigned = tokens[0] if tokens else None
    if assigned not in ROLES:
        assigned = None
    how = tokens[1] if len(tokens) > 1 else None
    if how not in HOW_TOKENS:
        how = None
    picks = [t for t in tokens[2:] if t in ROLES and t not in PLACEHOLDER_PICKS]
    return {"assigned": assigned, "how": how, "picks": picks}


def choice_badge(how) -> str | None:
    """Wie-Token -> Badge-Label ('OFFROLE'/'AUTOFILL'); Erstwahl -> None."""
    return CHOICE_BADGE.get(how or "")


def is_remake(info: dict) -> bool:
    """Remake? `gameEndedInEarlySurrender` bei einem Spiel unter 5 Minuten.

    Beides steht im bereits geladenen Match (0 Calls). Die Dauer ist die
    Absicherung: das Flag allein soll keine lange Partie zum Remake erklaeren."""
    parts = (info or {}).get("participants") or []
    if not any(bool(p.get("gameEndedInEarlySurrender")) for p in parts):
        return False
    try:
        duration = float((info or {}).get("gameDuration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return duration < REMAKE_MAX_SECONDS


# ============================================================================
# 3. Identitaet + Rang-Abruf (Cache-first)
# ============================================================================

def key_identity(client) -> str:
    """Kurzer Fingerabdruck des aktiven API-Keys (Cache-Schluessel-Bestandteil).

    Nicht zur Sicherheit, sondern zur TRENNUNG: PUUIDs sind key-gebunden, ein
    Cache-Eintrag eines anderen Keys darf nie wiederverwendet werden.

    Massgeblich ist `client.last_key` - der Key, der zuletzt tatsaechlich
    GEANTWORTET hat. `_keys[0]` gilt nur, solange noch keine Antwort kam:
    `fetch._FallbackClient` traegt dort bis zum ersten Call den `dev_api_key`,
    auch wenn der abgelaufen ist und alle Daten in Wahrheit vom `api_key`
    kommen. Der Proxy reicht nicht-aufrufbare Attribute durch, `last_key`
    stammt also immer vom aktuell aktiven RiotClient."""
    active = getattr(client, "last_key", None)
    if not active:
        keys = getattr(client, "_keys", None)
        if isinstance(keys, (list, tuple)) and keys:
            active = keys[0]
    if not active:
        return "nokey"
    digest = hashlib.sha1(str(active).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:8]


class RankLookup:
    """Cache-first-Zugriff auf account-v1 + league-v4 mit Call-Zaehler.

    Jeder Netz-Zugriff laeuft ueber `_api`: er zaehlt (`calls`), respektiert
    ein optionales Budget (`budget`/`spent`, nur die Peer-Schaetzung setzt es)
    und schluckt jeden Fehler - ein toter Endpunkt darf den Report nie
    abbrechen. Cache-Treffer kosten NICHTS und werden nicht gezaehlt; genau
    darauf zielen die Call-Zaehler-Tests."""

    def __init__(self, cfg, client, *, log=print):
        self.cfg = cfg
        self.client = client
        self.log = log
        self.calls = 0
        self.budget = None            # None = unbegrenzt (Grundlast)
        self.spent = 0
        self.budget_exhausted = False
        self.dead = False             # Riot hat alle Keys abgelehnt
        self._accounts = shardstore.shared(cfg, "accounts")
        self._ranks = shardstore.shared(cfg, "ranks")
        hours = getattr(cfg, "postgame_rank_ttl_hours", 12)
        try:
            self._rank_ttl = max(0.0, float(hours)) * 3600.0
        except (TypeError, ValueError):
            self._rank_ttl = 12 * 3600.0
        # Beide Memos sind identitaetsBEWUSST gekeyt ((key_id, ...)): waehrend
        # eines Reports kann der Client den Key wechseln (Dev-Key abgelehnt ->
        # api_key), und dann gilt kein einziger frueherer Eintrag mehr.
        self._acc_mem: dict = {}
        self._rank_mem: dict = {}

    @property
    def key_id(self) -> str:
        """Identitaet des AKTUELL antwortenden Keys (read-only, live berechnet).

        Bewusst kein in `__init__` eingefrorener Wert: `fetch._FallbackClient`
        wird mit dem `dev_api_key` gebaut und schaltet erst beim ersten
        abgelehnten Call auf den `api_key` um - eingefroren truege der Cache
        danach die Dev-Kennung fuer Daten des Haupt-Keys (s. Modul-Doc)."""
        return key_identity(self.client)

    # --- Budget ---

    def start_budget(self, limit) -> None:
        """Ab hier gilt ein Call-Deckel (Peer-Schaetzung). None = keiner."""
        self.budget = None if limit is None else max(0, int(limit))
        self.spent = 0

    def has_budget(self) -> bool:
        if self.dead:
            return False
        if self.budget is None:
            return True
        if self.spent >= self.budget:
            self.budget_exhausted = True
            return False
        return True

    # --- Netz ---

    def _api(self, call, *args, **kwargs):
        """EIN Riot-Call: gezaehlt, budgetiert, fehlertolerant."""
        if not self.has_budget():
            return None
        self.calls += 1
        self.spent += 1
        try:
            return call(*args, **kwargs)
        except SystemExit as exc:
            # RiotClient wirft SystemExit, wenn alle Keys abgelehnt sind.
            self.dead = True
            self.log(f"[fairness] Riot-API nicht mehr nutzbar ({exc}) - "
                     f"Rang-Teil bleibt unvollstaendig.")
            return None
        except Exception as exc:   # noqa: BLE001 - Sektion darf nie crashen
            self.log(f"[fairness] Riot-Call fehlgeschlagen ({exc!r}) - "
                     f"uebersprungen.")
            return None

    # --- Riot-ID -> PUUID ---

    def puuid_for(self, riot_id: str):
        """Riot-ID -> PUUID DIESES Keys (Cache-first). None, wenn unbekannt.

        Der Cache-Schluessel traegt die Key-Identitaet: der Eintrag eines
        anderen Keys darf nie wiederverwendet werden (er waere gegen jeden
        Endpunkt HTTP 400 wert).

        Die Identitaet wird ZWEIMAL gelesen - vor dem Call fuer Memo/Cache, nach
        dem Call fuer das Schreiben. Dazwischen kann der Proxy den Key gewechselt
        haben (Dev-Key abgelehnt -> `api_key`); die Antwort gehoert dann dem
        NEUEN Key und darf nur unter dessen Kennung abgelegt werden."""
        rid = str(riot_id or "").strip()
        if "#" not in rid:
            return None
        rid_lower = rid.lower()
        before = self.key_id
        if (before, rid_lower) in self._acc_mem:
            return self._acc_mem[(before, rid_lower)]
        cache_id = f"{before}|{rid_lower}"
        cached = self._accounts.get(cache_id)
        if (isinstance(cached, dict) and cached.get("puuid")
                and self._accounts.fresh(cache_id, ACCOUNT_TTL_S)):
            self._acc_mem[(before, rid_lower)] = cached["puuid"]
            return cached["puuid"]
        name, _, tag = rid.partition("#")
        acc = self._api(self.client.account_by_riot_id, name.strip(), tag.strip())
        puuid = acc.get("puuid") if isinstance(acc, dict) else None
        now = self.key_id                     # ggf. mitten im Call gewechselt
        if puuid:
            self._accounts.put(f"{now}|{rid_lower}", {"puuid": puuid})
        self._acc_mem[(now, rid_lower)] = puuid
        return puuid

    # --- PUUID -> Rang ---

    def entries_for(self, puuid: str):
        """Rohe league-v4-Entries einer PUUID (Cache-first). None = kein Abruf.

        Eine leere Liste ist ein GUELTIGES Ergebnis (unranked) und wird
        mitgecacht - sonst fragte jeder Report fuer dieselben Unranked erneut.

        Das Memo traegt die Key-Identitaet mit: nach einem Key-Wechsel ist ein
        fehlgeschlagener Abruf (fremde PUUID -> HTTP 400 -> None) kein Urteil
        mehr ueber die PUUID des neuen Keys. Der Shard-Store dagegen kommt ohne
        Kennung aus - die PUUID selbst ist bereits key-spezifisch."""
        if not puuid:
            return None
        memo_key = (self.key_id, puuid)
        if memo_key in self._rank_mem:
            return self._rank_mem[memo_key]
        if self._rank_ttl > 0 and self._ranks.fresh(puuid, self._rank_ttl):
            cached = self._ranks.get(puuid)
            if isinstance(cached, list):
                self._rank_mem[memo_key] = cached
                return cached
        entries = self._api(self.client.league_entries_by_puuid, puuid)
        if isinstance(entries, list):
            self._ranks.put(puuid, entries)
        else:
            entries = None
        self._rank_mem[memo_key] = entries
        return entries

    def rank_by_puuid(self, puuid: str):
        """Rang zu einer PUUID **unseres** Keys (Direktweg, kein account-v1)."""
        return rank_from_entries(self.entries_for(puuid))

    def rank_by_riot_id(self, riot_id: str):
        """Rang zu einer Riot-ID (der key-sichere Regelweg, s. Modul-Doc).

        Wechselt der Client MITTEN im Abruf den Key (Dev-Key abgelehnt ->
        `api_key`), gehoerte die zuerst benutzte PUUID noch zum alten Key -
        Riot quittiert sie beim neuen mit HTTP 400, `entries_for` liefert None.
        Dann folgt genau EIN zweiter Anlauf unter der neuen Identitaet:
        `puuid_for` geht dafuer an deren Cache-Eintrag bzw. an account-v1, weil
        die Memos identitaetsbewusst gekeyt sind."""
        before = self.key_id
        entries = self.entries_for(self.puuid_for(riot_id))
        if entries is None and self.key_id != before:
            entries = self.entries_for(self.puuid_for(riot_id))
        return rank_from_entries(entries)


def ranks_for(lookup: RankLookup, riot_ids, *, progress=NOOP) -> dict:
    """{Riot-ID: Rang-Dict|None} fuer eine Folge von Riot-IDs.

    Bewusst Riot-ID- statt PUUID-basiert (Abweichung vom ersten Plan-Entwurf):
    die PUUIDs im Match stammen moeglicherweise aus dem Cache und gehoeren dann
    einem fremden Key - s. Identitaets-Regel im Modul-Docstring.

    `progress` bekommt je aufgeloester Riot-ID einen `tick()` - auch bei einem
    Cache-Treffer: fuer den Ladebalken zaehlt, dass der Spieler erledigt ist,
    nicht ob er einen Call gekostet hat."""
    out: dict = {}
    for rid in riot_ids:
        if not rid or rid in out:
            continue
        out[rid] = lookup.rank_by_riot_id(rid)
        progress.tick()
    return out


# ============================================================================
# 4. Peer-Schaetzung fuer unranked Spieler (F-03c)
# ============================================================================

def _match_for_peers(cfg, lookup: RankLookup, match_id: str):
    """(match, selbst_geholt) - Cache-first, sonst EIN Match-Call.

    `selbst_geholt=True` heisst: die PUUIDs dieses Matches gehoeren unserem Key
    und sind direkt gegen league-v4 verwendbar (spart den account-v1-Schritt).
    Aus dem Cache gelesene Matches tragen dagegen die PUUIDs des Keys, der sie
    geholt hat - dort fuehrt nur die Riot-ID weiter."""
    try:
        _patch, cached = fetch._find_cached(cfg, "matches", match_id)
    except Exception:   # noqa: BLE001 - kaputter Cache darf nichts brechen
        cached = None
    if isinstance(cached, dict) and cached.get("info"):
        return cached, False
    data = lookup._api(lookup.client.match, match_id)
    if not isinstance(data, dict) or not data.get("info"):
        return None, False
    try:
        patch = ddragon.patch_of(data["info"].get("gameVersion", ""))
        fetch._cache(cfg, "matches", patch, match_id, data)
    except Exception:   # noqa: BLE001 - Cachen ist Kuer
        pass
    return data, True


def estimate_depth(cfg) -> int:
    """Wie viele Referenzspiele je unranked Spieler ausgewertet werden.

    Config (`postgame.estimate_matches`) vor Modul-Default `PEER_MATCHES`;
    unbrauchbare Werte fallen auf den Default zurueck, Minimum ist 1."""
    try:
        return max(1, int(getattr(cfg, "postgame_estimate_matches", PEER_MATCHES)))
    except (TypeError, ValueError):
        return PEER_MATCHES


def _budget_for(cfg) -> int:
    """Call-Deckel der Schaetzung: explizit gesetzt schlaegt abgeleitet.

    Ohne `postgame.estimate_budget_calls` (Feld-Default None) waechst das Budget
    mit der Tiefe - `BUDGET_PER_MATCH` Calls je Referenzspiel. Ein fixer Deckel
    wuerde sonst bei tieferer Suche VOR der Tiefe greifen und die Einstellung
    wirkungslos machen."""
    explicit = getattr(cfg, "postgame_estimate_budget_calls", None)
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass   # unbrauchbarer Wert -> abgeleitetes Budget
    return BUDGET_PER_MATCH * estimate_depth(cfg)


def estimate_rank(cfg, lookup: RankLookup, puuid: str, *, exclude=()) -> dict | None:
    """Rang eines unranked Spielers aus seinen Mitspielern schaetzen.

    Design (F-03c): `estimate_depth(cfg)` Referenzspiele (ALLE SR-5v5-Queues,
    nicht nur Ranked - wer unranked ist, spielt kaum Ranked), je `PEER_PER_MATCH`
    Mitspieler deterministisch nach `participantId`, Median ihrer Scores.

    Die Match-IDs werden mit Reserve geholt (`PEER_SCAN_FACTOR`-faches der
    Tiefe, derselbe Call): Nicht-SR-Spiele (ARAM, Arena) erkennt man erst nach
    dem Match-Fetch, ohne Reserve zaehlten sie als verbrauchtes Referenzspiel.
    Die Schleife bricht ab, sobald die Tiefe voll ist, die Liste endet oder das
    Budget (`_budget_for`) erschoepft ist - untersucht werden also hoechstens
    Tiefe * `PEER_SCAN_FACTOR` Matches.

    `exclude` ist die Menge der bereits vergebenen bzw. verbotenen Identitaeten
    (kleingeschriebene volle Riot-IDs): der Spieler selbst, alle Teilnehmer des
    analysierten Matches - sonst schaetzen sich Lobby-Mitglieder gegenseitig -
    und, laufend ergaenzt, die schon gezogenen Peers (Dedup).

    Rueckgabe None, wenn kein einziger Peer-Rang zusammenkam."""
    if not puuid or not lookup.has_budget():
        return None
    depth = estimate_depth(cfg)
    scan = depth * PEER_SCAN_FACTOR
    ids = lookup._api(lookup.client.match_ids, puuid, queue=None,
                      count=scan, type_filter=None) or []
    candidates = []
    for mid in list(ids)[:scan]:
        if len(candidates) >= depth or not lookup.has_budget():
            break
        match, fresh = _match_for_peers(cfg, lookup, mid)
        if match is None:
            continue
        queue_id = (match.get("info") or {}).get("queueId")
        if queue_id not in fetch.SR_5V5_QUEUES:
            continue
        candidates.append((match, fresh, queue_id in RANKED_QUEUES))
    # Ranked-Referenzspiele zuerst auswerten (stabil, kein Zufall): reisst das
    # Budget mittendrin, ist die bessere Quelle bereits drin.
    candidates.sort(key=lambda c: not c[2])

    blocked = set(exclude)
    scores: list = []
    peers = 0
    from_ranked = False
    for match, fresh, ranked in candidates:
        picked = 0
        parts = sorted((match.get("info") or {}).get("participants") or [],
                       key=lambda p: p.get("participantId") or 0)
        for part in parts:
            if picked >= PEER_PER_MATCH:
                break
            rid = riot_id_of(part)
            ident = rid.lower()
            if not ident or ident in blocked:
                continue
            if not lookup.has_budget():
                break
            blocked.add(ident)
            picked += 1
            peers += 1
            rank = (lookup.rank_by_puuid(part.get("puuid")) if fresh
                    else lookup.rank_by_riot_id(rid))
            if rank:
                scores.append(rank["score"])
                from_ranked = from_ranked or ranked
    if not scores:
        return None
    score = int(round(statistics.median(sorted(scores))))
    return {"score": score, "label": score_label(score), "estimated": True,
            "basis": len(scores), "peers": peers, "from_ranked": from_ranked,
            "source": "estimate", "tier": None, "division": None, "lp": None,
            "queue": None}


# ============================================================================
# 5. Zusammenbau + Verdikt (F-03d)
# ============================================================================

def _client_for(cfg, match_id: str, log=print):
    """Postgame-Client (Dev-Key-Vorrang) oder None, wenn kein Key da ist."""
    primary, _fallback = cfg.postgame_keys
    if not primary:
        return None
    try:
        return fetch._build_client(cfg, match_id)
    except Exception as exc:   # noqa: BLE001 - ohne Client bleibt der Rang leer
        log(f"[fairness] Kein Riot-Client ({exc!r}) - Sektion ohne Raenge.")
        return None


def _side(part: dict, rank, my_team: int, me_pid) -> dict:
    """Eine Seite einer Rollen-Zeile (Name, Champ, Wahl-Badge, Level, Rang)."""
    choice = role_choice(part)
    try:
        level = int(part.get("summonerLevel") or 0)
    except (TypeError, ValueError):
        level = 0
    return {
        "pid": part.get("participantId"),
        "name": riot_id_of(part) or str(part.get("riotIdGameName") or ""),
        "champ": part.get("championName") or "",
        "level": level,
        "choice": choice["how"],
        "badge": choice_badge(choice["how"]),
        "picks": choice["picks"],
        "rank": rank,
        "is_me": part.get("participantId") == me_pid,
        "team": part.get("teamId"),
        "own": part.get("teamId") == my_team,
    }


def _team_mean(sides: list) -> dict:
    """Team-Mittel: geschaetzte Raenge zaehlen mit, echte Unranked nicht.

    Die Basis wird ausgewiesen (`measured`/`estimated`/`missing`) - ein Mittel
    aus zwei von fuenf Werten ist etwas anderes als eines aus fuenf, und das
    darf die Sektion nicht verschweigen."""
    scores, measured, estimated, missing = [], 0, 0, 0
    for side in sides:
        rank = side.get("rank") if side else None
        if not rank:
            missing += 1
            continue
        scores.append(rank["score"])
        if rank.get("estimated"):
            estimated += 1
        else:
            measured += 1
    score = (sum(scores) / len(scores)) if scores else None
    return {"score": score, "label": score_label(score) if scores else None,
            "measured": measured, "estimated": estimated, "missing": missing,
            "n": len(scores)}


def _delta(me_rank, opp_rank):
    """(Punkte-Differenz, geschaetzt?) eines Paars - None, wenn eine Seite fehlt."""
    if not me_rank or not opp_rank:
        return None, False
    est = bool(me_rank.get("estimated") or opp_rank.get("estimated"))
    return me_rank["score"] - opp_rank["score"], est


def _divisions(points) -> float:
    return round(points / DIVISION_POINTS, 1)


def _de(value) -> str:
    """Zahl mit deutschem Dezimalkomma (Verdikt-Text)."""
    return f"{value:.1f}".replace(".", ",")


def _verdict(mean: dict, autofill: dict, flex: int, estimated: int,
             missing: int, budget_note) -> list:
    """Beschreibende Verdikt-Zeilen - ohne Kausalitaet zum Spielausgang.

    Die Sektion beschreibt die ZUSAMMENSTELLUNG. Ein Gefaelle erklaert weder
    Sieg noch Niederlage, und ein Autofill ist eine Tatsache aus den Match-
    Daten, keine Schuldzuweisung."""
    lines = []
    delta = mean.get("delta")
    if delta is None:
        lines.append("Für den Rang-Vergleich lagen zu wenige Einstufungen vor — "
                     "die Sektion zeigt nur Rollen-Wahl und Account-Level.")
    else:
        div = abs(_divisions(delta))
        higher = "dein Team" if delta > 0 else "die Gegenseite"
        if div < EVEN_MAX_DIV:
            lines.append(f"Ausgeglichen: beide Seiten liegen im Mittel weniger "
                         f"als eine halbe Division auseinander ({_de(div)}).")
        elif div <= SLIGHT_MAX_DIV:
            lines.append(f"Leichtes Gefälle: {higher} stand im Schnitt "
                         f"{_de(div)} Divisionen höher.")
        else:
            lines.append(f"Deutliches Gefälle: {higher} stand im Schnitt "
                         f"{_de(div)} Divisionen höher.")
    total_fill = autofill.get("me", 0) + autofill.get("opp", 0)
    if total_fill:
        lines.append(f"Rolle nicht als Erstwahl bekommen: "
                     f"{autofill.get('me', 0)} in deinem Team, "
                     f"{autofill.get('opp', 0)} auf der Gegenseite.")
    if estimated:
        lines.append(f"{estimated} der Einstufungen sind Schätzungen aus "
                     f"Mitspielern vergangener Spiele — die Größenordnung "
                     f"trägt, der Einzelwert nicht.")
    # Der Nicht-Befund "gar kein Rang da" steht schon in der ersten Zeile - ihn
    # zusaetzlich als Fehlmenge zu melden waere eine Tautologie.
    known = (mean.get("me") or {}).get("n", 0) + (mean.get("opp") or {}).get("n", 0)
    if missing and known:
        lines.append(f"Für {missing} Spieler ließ sich kein Rang bestimmen; "
                     f"sie zählen nicht ins Mittel.")
    if flex > 1:
        lines.append(f"{flex} Einstufungen stammen aus der Flex-Queue — sie "
                     f"werden nicht umgerechnet und sind mit Solo/Duo nur "
                     f"grob vergleichbar.")
    if budget_note:
        lines.append(budget_note)
    return lines


def _skip_rank_phases(progress) -> None:
    """Beide Rang-Phasen aus dem Fortschritts-Plan nehmen.

    Gilt fuer jeden Weg, auf dem die Sektion keinen einzigen Riot-Call macht
    (abgeschaltet, keine Participants, Remake, kein Client): der Balken soll
    dann nicht bei 25 % haengen bleiben, sondern die restlichen Phasen den
    ganzen Weg abdecken."""
    progress.skip("ranks")
    progress.skip("estimate")


def build_fairness(cfg, match: dict, my_team: int, me_pid, *, match_id=None,
                   client=None, log=print, progress=NOOP) -> dict | None:
    """Fairness-Modell fuer den Report - oder None, wenn die Sektion entfaellt.

    None kommt bei abgeschalteter Sektion (`postgame.fairness: false`), bei
    einem Remake (Guard VOR jeder Identitaets-Aufloesung, also 0 Calls) und
    bei einem Match ohne Participants. Alles Uebrige degradiert: ohne API-Key
    bleiben die Rang-Spalten leer, die Rollen-Wahl steht trotzdem da.

    `progress` ist der Ladebalken-Transport: diese Sektion macht praktisch alle
    Riot-Calls des Reports und traegt darum die beiden teuersten Phasen
    ("ranks"/"estimate"). Faellt eine davon aus (kein Client, kein unranked
    Spieler, Sektion entfaellt ganz), wird sie mit `skip()` aus dem Plan
    genommen - ihr Anteil verteilt sich dann auf die uebrigen Phasen, statt als
    Luecke im Balken zu bleiben."""
    if not getattr(cfg, "postgame_fairness", True):
        _skip_rank_phases(progress)
        return None
    info = (match or {}).get("info") or {}
    parts = info.get("participants") or []
    if not parts:
        _skip_rank_phases(progress)
        return None
    if is_remake(info):
        _skip_rank_phases(progress)
        return None

    if client is None:
        mid = (match_id or (match.get("metadata") or {}).get("matchId")
               or f"{cfg.platform.upper()}_0")
        client = _client_for(cfg, mid, log=log)

    lookup = None
    ranks: dict = {}
    if client is None:
        # Ohne Client kostet die Sektion keinen einzigen Call - beide
        # Rang-Phasen entfallen, die Sektion selbst entsteht trotzdem
        # (Rollen-Wahl + Level sind offline da).
        _skip_rank_phases(progress)
    else:
        lookup = RankLookup(cfg, client, log=log)
        riot_ids = [riot_id_of(p) for p in parts]
        progress.phase("ranks", "Ränge {done}/{total}",
                       total=len({r for r in riot_ids if r}))
        ranks = ranks_for(lookup, riot_ids, progress=progress)

    # --- Peer-Schaetzung fuer alles, was weder Solo noch Flex hat ------------
    budget_note = None
    estimate_wanted = 0
    estimate_done = 0
    if lookup is not None and getattr(cfg, "postgame_estimate_unranked", True):
        lobby = {riot_id_of(p).lower() for p in parts if riot_id_of(p)}
        unranked = [p for p in sorted(parts, key=lambda x: x.get("participantId") or 0)
                    if riot_id_of(p) and not ranks.get(riot_id_of(p))]
        estimate_wanted = len(unranked)
        if unranked:
            progress.phase("estimate", "Rang-Schätzung {done}/{total}",
                           total=estimate_wanted)
            lookup.start_budget(_budget_for(cfg))
            for part in unranked:
                rid = riot_id_of(part)
                if not lookup.has_budget():
                    # Budget alle: der Zaehler bleibt stehen, wo er steht -
                    # ehrlicher als ihn auf "fertig" zu ziehen.
                    break
                guess = estimate_rank(cfg, lookup, lookup.puuid_for(rid),
                                      exclude=lobby)
                if guess:
                    ranks[rid] = guess
                    estimate_done += 1
                # Auch ohne Treffer ist dieser Spieler abgearbeitet (er kostete
                # trotzdem Calls) - der Balken zaehlt Spieler, nicht Erfolge.
                progress.tick(note=f"Calls {lookup.spent}/{lookup.budget}")
            lookup.start_budget(None)
            if lookup.budget_exhausted and estimate_done < estimate_wanted:
                budget_note = (f"Schätzung für {estimate_done} von "
                               f"{estimate_wanted} Spielern — danach war das "
                               f"Call-Budget erschöpft.")
    if lookup is not None and not estimate_wanted:
        # Nichts zu schaetzen (voll eingestufte Lobby oder Schaetzung
        # abgeschaltet) -> Phase raus aus dem Plan.
        progress.skip("estimate")

    # --- Paare je Rolle (analog analysis.build_scoreboard) -------------------
    from . import analysis   # lokal: vermeidet einen Import-Zyklus beim Laden

    by_role_team: dict = {}
    for part in parts:
        role = part.get("teamPosition") or ""
        if role:
            by_role_team[(role, part.get("teamId"))] = part
    other_team = 200 if my_team == 100 else 100
    my_roles = sorted({r for (r, t) in by_role_team if t == my_team},
                      key=lambda r: analysis.ROLE_ORDER.get(r, 9))

    rows = []
    me_sides, opp_sides = [], []
    autofill = {"me": 0, "opp": 0}
    flex = 0
    for role in my_roles:
        me_part = by_role_team.get((role, my_team))
        opp_part = by_role_team.get((role, other_team))
        me_side = (_side(me_part, ranks.get(riot_id_of(me_part)), my_team, me_pid)
                   if me_part else None)
        opp_side = (_side(opp_part, ranks.get(riot_id_of(opp_part)), my_team, me_pid)
                    if opp_part else None)
        if me_side:
            me_sides.append(me_side)
            if me_side["badge"] == "AUTOFILL":
                autofill["me"] += 1
        if opp_side:
            opp_sides.append(opp_side)
            if opp_side["badge"] == "AUTOFILL":
                autofill["opp"] += 1
        for side in (me_side, opp_side):
            if side and (side.get("rank") or {}).get("source") == "flex":
                flex += 1
        points, estimated = _delta(me_side and me_side["rank"],
                                   opp_side and opp_side["rank"])
        rows.append({"role": role, "me": me_side, "opp": opp_side,
                     "delta": points,
                     "delta_div": None if points is None else _divisions(points),
                     "estimated": estimated})

    mean_me = _team_mean(me_sides)
    mean_opp = _team_mean(opp_sides)
    mean_delta = (None if mean_me["score"] is None or mean_opp["score"] is None
                  else mean_me["score"] - mean_opp["score"])
    mean = {
        "me": mean_me, "opp": mean_opp, "delta": mean_delta,
        "delta_div": None if mean_delta is None else _divisions(mean_delta),
        "delta_tiers": (None if mean_delta is None
                        else round(mean_delta / TIER_POINTS, 1)),
        "estimated": bool(mean_me["estimated"] or mean_opp["estimated"]),
    }
    estimated_total = mean_me["estimated"] + mean_opp["estimated"]
    missing_total = mean_me["missing"] + mean_opp["missing"]
    return {
        "rows": rows,
        "mean": mean,
        "autofill": autofill,
        "flex": flex,
        "estimated": estimated_total,
        "missing": missing_total,
        "budget_exhausted": bool(lookup is not None and lookup.budget_exhausted),
        "budget_note": budget_note,
        "calls": lookup.calls if lookup is not None else 0,
        "verdict": _verdict(mean, autofill, flex, estimated_total,
                            missing_total, budget_note),
    }
