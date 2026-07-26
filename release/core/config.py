"""Konfiguration der Wissensbasis-Pipeline.

API-Key und Rate-Limits kommen aus config.yml im Projektverzeichnis;
die Umgebungsvariable RIOT_API_KEY ueberschreibt den Key aus der Datei.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

VALID_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
_ROLE_ALIASES = {"JG": "JUNGLE", "JGL": "JUNGLE", "JUNGLER": "JUNGLE",
                 "MID": "MIDDLE", "BOT": "BOTTOM", "ADC": "BOTTOM",
                 "SUP": "UTILITY", "SUPP": "UTILITY", "SUPPORT": "UTILITY"}


def normalize_role(value: str) -> str:
    """Beliebige Schreibweise ('jungle', 'Mid', 'adc') -> teamPosition der
    Match-API ('JUNGLE', 'MIDDLE', 'BOTTOM'). Unbekannte Werte brechen ab."""
    role = str(value).strip().upper()
    role = _ROLE_ALIASES.get(role, role)
    if role not in VALID_ROLES:
        raise SystemExit(f"Unbekannte Rolle '{value}' - gueltig: "
                         f"{', '.join(VALID_ROLES)} (Gross-/Kleinschreibung egal).")
    return role


# Seed-Leiter, AUFSTEIGEND geordnet (unterste Stufe zuerst). Die Riot-API kennt
# nur Tier+Division-Granularitaet (kein LP-Filter), der Regler ist also eine
# Leiter-Stufe. Apex-Tiers (Master/GM/Challenger) haben keine Divisionen.
SEED_LADDER = ("PLATINUM_IV", "PLATINUM_III", "PLATINUM_II", "PLATINUM_I",
               "EMERALD_IV", "EMERALD_III", "EMERALD_II", "EMERALD_I",
               "DIAMOND_IV", "DIAMOND_III", "DIAMOND_II", "DIAMOND_I",
               "MASTER", "GRANDMASTER", "CHALLENGER")

# Roemisch <-> arabisch fuer Diamond-Divisionen.
_DIVISION_ARABIC = {"I": "1", "II": "2", "III": "3", "IV": "4"}
_DIVISION_ROMAN = {v: k for k, v in _DIVISION_ARABIC.items()}

# Kurze Log-Labels fuer bekannte Platforms.
_REGION_LABELS: dict[str, str] = {
    "euw1": "EUW", "eun1": "EUNE", "na1": "NA", "kr": "KR",
    "br1": "BR", "jp1": "JP", "oc1": "OCE", "la1": "LAN",
    "la2": "LAS", "tr1": "TR", "ru": "RU",
}


def normalize_ladder_step(value: str) -> str:
    """Beliebige Schreibweise einer Leiter-Stufe -> kanonischer SEED_LADDER-Wert.

    Akzeptiert Apex-Tiers ('master', 'Grandmaster', 'challenger') sowie
    Diamond/Emerald/Platinum in arabischer UND roemischer Division plus
    Kurzformen: 'diamond_1', 'd1', 'emerald_iv', 'e1', 'platinum_1', 'p1'
    -> 'DIAMOND_I', 'EMERALD_IV', 'PLATINUM_I'. Unbekannte Werte brechen ab.
    """
    raw = str(value).strip().upper()
    # Trenner (Space, Bindestrich, Unterstrich) vereinheitlichen.
    token = raw.replace("-", "_").replace(" ", "_")
    while "__" in token:
        token = token.replace("__", "_")

    apex = {"MASTER": "MASTER", "GRANDMASTER": "GRANDMASTER", "GM": "GRANDMASTER",
            "CHALLENGER": "CHALLENGER", "CHALL": "CHALLENGER", "CHALLY": "CHALLENGER"}
    if token in apex:
        return apex[token]

    # Tiers mit Divisionen: Praefix (voller Name oder Kurzform) + Division
    # (roemisch oder arabisch), mit oder ohne Trenner.
    tier = None
    div_raw = None
    for prefix, tier_name in [("PLATINUM", "PLATINUM"), ("EMERALD", "EMERALD"),
                               ("DIAMOND", "DIAMOND")]:
        if token.startswith(prefix):
            tier = tier_name
            div_raw = token[len(prefix):].lstrip("_")
            break
    if tier is None:
        for short, tier_name in [("P", "PLATINUM"), ("E", "EMERALD"),
                                  ("D", "DIAMOND")]:
            if token.startswith(short) and len(token) > len(short):
                tier = tier_name
                div_raw = token[len(short):].lstrip("_")
                break
    if tier is not None:
        div = _DIVISION_ROMAN.get(div_raw, div_raw)   # arabisch -> roemisch
        if div in _DIVISION_ARABIC:                    # gueltige roemische Division
            return f"{tier}_{div}"

    raise SystemExit(
        f"Unbekannte Leiter-Stufe '{value}' - gueltig (Gross-/Kleinschreibung "
        f"egal, roemisch oder arabisch): {', '.join(SEED_LADDER)}.")


@dataclass
class Config:
    platform: str = "euw1"          # Platform-Host (league-v4, summoner-v4)
    routing: str = "europe"         # Regional-Host (match-v5)
    queue: int = 420                # Ranked Solo/Duo
    seed_start: str = "DIAMOND_I"   # Seed-Scan von hier AUFWAERTS
    seed_stop: str = "GRANDMASTER"  # ... bis hier (einschliesslich)
    max_players: int = 400          # wie viele Seed-Spieler maximal
    seed_ttl_hours: int = 24        # TTL fuer den Seed-Cache (players_*.json);
                                    # 0 = nie ablaufen (altes Verhalten)
    ids_per_player: int = 20        # wie viele Match-IDs pro Spieler
    max_matches: int = 3000         # Ziel: so viele Matches des aktuellen Patches
    min_games: int = 10             # Mindest-Sample pro Champion+Rolle im Output
    rate_limit_per_sec: int = 20
    rate_limit_per_2min: int = 100
    focus_role: str = "JUNGLE"
    focus_champions: tuple = ()
    focus_target_games: int = 200
    focus_min_mastery_level: int = 10  # Kandidaten mit weniger Mastery ausschliessen
    focus_pool_ttl_days: int = 90   # TTL fuer den patch-unabhaengigen Champion-Pool
    refresh_seconds: int = 60       # Frontend-Refresh-Intervall (app: in config.yml)
    auto_asset_download: bool = False  # beim Serverstart fehlende Item-Icons laden
    dump_live_data: bool = False    # Live-Rohdaten + berechnete Zustaende archivieren
    dump_interval_seconds: int = 5  # Poller-Kadenz bei aktivem Dump (Sekunden)
    cache_dir: Path = field(default_factory=lambda: ROOT / "data" / "pipeline")
    out_dir: Path = field(default_factory=lambda: ROOT / "knowledge" / "generated")
    postgame_out_dir: Path = field(default_factory=lambda: ROOT / "postgame")
    postgame_auto_on_end: bool = True   # Auto-Report bei Spielende (nur Voll-Modus:
                                        # greift nur mit Key + me, sonst No-Op)
    postgame_poll_interval_seconds: int = 20  # Kadenz der Spielende-Erkennung (Sek.)
    postgame_enrich_retries: int = 20   # Stufe-2-Versuche, bis Match indexiert ist
    postgame_enrich_backoff_seconds: int = 30  # Wartezeit zwischen den Versuchen
                                        # (Default 20x30 s ~ 10 min Gesamtbudget)
    me: str = ""                    # eigene Identitaet: Riot-ID 'Name#Tag' ODER PUUID
    api_key: str = ""
    dev_api_key: str = ""
    round_robin: bool = False
    regions: tuple = ()   # tuple[tuple[str, str], ...] — (platform, routing)-Paare

    @property
    def api_keys(self) -> tuple[str, ...]:
        """Deduplizierte Tuple aller konfigurierten API-Keys (api_key zuerst)."""
        seen: set[str] = set()
        keys: list[str] = []
        for k in (self.api_key, self.dev_api_key):
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        return tuple(keys)

    @property
    def active_api_keys(self) -> tuple[str, ...]:
        """Keys fuer den RiotClient: alle bei round_robin, sonst nur der erste."""
        keys = self.api_keys
        if self.round_robin:
            return keys
        return keys[:1]

    @property
    def postgame_keys(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(primary, fallback)-Keys fuer Postgame-Fetches (Match/Timeline,
        --latest, Enrichment). Unabhaengig von den Crawl-Keys (active_api_keys).

        Dev-Key-Vorrang (Nutzer-Entscheidung 2026-07-24): laeuft parallel ein
        langer Pipeline-Crawl (`focus` etc.) mit dem Haupt-`api_key`, sollen die
        Postgame-Fetches dessen Rate-Limit-Budget (100 Req/2 min) NICHT
        anknabbern -> sie bevorzugen den `dev_api_key`.

        - `round_robin: true` -> beide Keys wie die Crawls (`active_api_keys`),
          KEIN Dev-Vorrang und kein Fallback; wer round_robin einschaltet, will
          die Keys explizit buendeln (auch fuer Postgame).
        - sonst mit gesetztem `dev_api_key` -> primary = **nur** der Dev-Key
          (strikt, KEIN Round-Robin ueber beide - das wuerde den Haupt-Key
          mitbelasten); fallback = `api_key` (greift, wenn Riot den Dev-Key
          ablehnt, z. B. nach dem 24h-Ablauf).
        - sonst -> primary = `api_key` wie bisher, kein Fallback.
        """
        if self.round_robin:
            return self.active_api_keys, ()
        if self.dev_api_key:
            fallback = (self.api_key,) if self.api_key else ()
            return (self.dev_api_key,), fallback
        return self.active_api_keys, ()

    @property
    def seed_steps(self) -> tuple[str, ...]:
        """Teilbereich der SEED_LADDER von seed_start bis seed_stop
        einschliesslich, AUFSTEIGEND. seed_start == seed_stop (eine Stufe) ist
        gueltig; liegt seed_start oberhalb von seed_stop -> Abbruch."""
        lo = SEED_LADDER.index(self.seed_start)
        hi = SEED_LADDER.index(self.seed_stop)
        if lo > hi:
            raise SystemExit(
                f"seed_start '{self.seed_start}' liegt oberhalb von seed_stop "
                f"'{self.seed_stop}' - der Scan laeuft aufsteigend, bitte "
                f"seed_start <= seed_stop waehlen (Leiter: {', '.join(SEED_LADDER)}).")
        return SEED_LADDER[lo:hi + 1]

    @staticmethod
    def region_label(platform: str) -> str:
        """Kurzes Log-Praefix fuer eine Platform (z. B. 'euw1' -> 'EUW')."""
        label = _REGION_LABELS.get(platform.strip().lower())
        if label:
            return label
        # Fallback: Grossbuchstaben, abschliessende Ziffern entfernen.
        cleaned = platform.strip().upper().rstrip("0123456789")
        return cleaned or platform.strip().upper()

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        config_file = ROOT / "config.yml"
        if config_file.exists():
            data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            riot = data.get("riot", {})
            cfg.api_key = riot.get("api_key", "")
            cfg.dev_api_key = riot.get("dev_api_key", "")
            cfg.round_robin = bool(riot.get("round_robin", cfg.round_robin))
            cfg.rate_limit_per_sec = riot.get("rate_limit_per_sec", cfg.rate_limit_per_sec)
            cfg.rate_limit_per_2min = riot.get("rate_limit_per_2min", cfg.rate_limit_per_2min)
            app = data.get("app", {})
            try:
                cfg.refresh_seconds = max(2, int(app.get("refresh_seconds",
                                                         cfg.refresh_seconds)))
            except (TypeError, ValueError):
                pass  # unbrauchbarer Wert -> Default behalten
            cfg.auto_asset_download = bool(app.get("auto_asset_download",
                                                   cfg.auto_asset_download))
            cfg.dump_live_data = bool(app.get("dump_live_data",
                                              cfg.dump_live_data))
            try:
                cfg.dump_interval_seconds = max(1, int(app.get(
                    "dump_interval_seconds", cfg.dump_interval_seconds)))
            except (TypeError, ValueError):
                pass  # unbrauchbarer Wert -> Default behalten
            pipeline = data.get("pipeline", {})
            if "seed_start" in pipeline:
                cfg.seed_start = normalize_ladder_step(pipeline["seed_start"])
            if "seed_stop" in pipeline:
                cfg.seed_stop = normalize_ladder_step(pipeline["seed_stop"])
            if "seed_ttl_hours" in pipeline:
                try:
                    cfg.seed_ttl_hours = max(0, int(pipeline["seed_ttl_hours"]))
                except (TypeError, ValueError):
                    pass  # unbrauchbarer Wert -> Default (24 h) behalten
            # Eigene Identitaet fuer den Post-Game-Report (wer bin "ich" im
            # Match). Top-Level `me:` (Riot-ID 'Name#Tag' oder PUUID). Optionaler
            # Ausgabeordner unter postgame.out_dir (Default ROOT/postgame).
            cfg.me = str(data.get("me", cfg.me) or "").strip()
            postgame = data.get("postgame", {}) or {}
            if postgame.get("out_dir"):
                p = Path(str(postgame["out_dir"]))
                cfg.postgame_out_dir = p if p.is_absolute() else ROOT / p
            # Auto-Trigger nach Spielende (Phase 2). Default an, wirkt aber nur im
            # Voll-Modus (Key + me) - fehlt eines, ist der Trigger ein No-Op.
            cfg.postgame_auto_on_end = bool(postgame.get("auto_on_end",
                                                         cfg.postgame_auto_on_end))
            try:
                cfg.postgame_poll_interval_seconds = max(5, int(postgame.get(
                    "poll_interval_seconds", cfg.postgame_poll_interval_seconds)))
            except (TypeError, ValueError):
                pass  # unbrauchbarer Wert -> Default (20 s) behalten
            # Stufe-2-Retry-Budget (Schaden-Anreicherung). Match-V5 indexiert oft
            # erst Minuten nach Spielende - reichlich Versuche geben. Backoff min.
            # 5 s, damit das Riot-Rate-Limit nicht ueberrannt wird.
            try:
                cfg.postgame_enrich_retries = max(1, int(postgame.get(
                    "enrich_retries", cfg.postgame_enrich_retries)))
            except (TypeError, ValueError):
                pass  # unbrauchbarer Wert -> Default (20) behalten
            try:
                cfg.postgame_enrich_backoff_seconds = max(5, int(postgame.get(
                    "enrich_backoff_seconds", cfg.postgame_enrich_backoff_seconds)))
            except (TypeError, ValueError):
                pass  # unbrauchbarer Wert -> Default (30 s) behalten
            focus = data.get("focus", {})
            cfg.focus_role = normalize_role(focus.get("role", cfg.focus_role))
            cfg.focus_champions = tuple(focus.get("champions", []))
            cfg.focus_target_games = focus.get("target_games", cfg.focus_target_games)
            cfg.focus_min_mastery_level = focus.get("min_mastery_level",
                                                    cfg.focus_min_mastery_level)
            cfg.focus_pool_ttl_days = focus.get("pool_ttl_days",
                                                cfg.focus_pool_ttl_days)
            # --- regions (Multi-Region-Vorbereitung) ---
            raw_regions = riot.get("regions", [])
            if raw_regions:
                parsed: list[tuple[str, str]] = []
                for i, entry in enumerate(raw_regions):
                    if not isinstance(entry, dict):
                        raise SystemExit(
                            f"riot.regions[{i}]: Eintrag muss ein dict mit "
                            f"'platform' und 'routing' sein, ist aber {type(entry).__name__}.")
                    p = entry.get("platform")
                    r = entry.get("routing")
                    if not p or not r:
                        raise SystemExit(
                            f"riot.regions[{i}]: 'platform' und 'routing' muessen "
                            f"beide angegeben sein (erhalten: platform={p!r}, routing={r!r}).")
                    parsed.append((str(p).strip().lower(), str(r).strip().lower()))
                cfg.regions = tuple(parsed)
        cfg.api_key = os.environ.get("RIOT_API_KEY", cfg.api_key)
        # Offline-Override fuer den Release-Smoketest / netzfreie Starts: ist
        # LOL_SKIP_ASSET_DOWNLOAD truthy gesetzt, wird der Asset-Download beim
        # Serverstart erzwungen deaktiviert (ueberschreibt config.yml).
        if os.environ.get("LOL_SKIP_ASSET_DOWNLOAD", "").strip().lower() \
                not in ("", "0", "false", "no"):
            cfg.auto_asset_download = False
        # Fallback: ohne regions-Block -> einzige Region aus platform/routing.
        if not cfg.regions:
            cfg.regions = ((cfg.platform, cfg.routing),)
        return cfg
