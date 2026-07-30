"""Champion-basierter Schadens-Prior (AD/AP), unabhaengig von Live-Items.

Der Schadenstyp eines Gegners haengt am Champion, nicht an seinen Items:
Ahri macht magischen Schaden, egal was sie baut; ein Full-Tank-Malphite hat
kaum Offensiv-Gold, sein Schaden bleibt magisch. Data Dragon liefert pro
Champion `info.attack` / `info.magic` (0-10) - daraus leiten wir einen
A-priori-Split ab, der ab Minute 0 verfuegbar ist. Live-Items verfeinern
diesen Prior spaeter nur noch (siehe engine/profiling.py).
"""

from functools import lru_cache
from pathlib import Path

from core import ddragon


# Kuratierte Korrekturen (Data-Dragon-ID -> ad_share), wo die info-Werte das
# echte Schadensprofil klar verfehlen. Bewusst klein und konservativ gehalten -
# bei Unsicherheit lieber weglassen, der Prior muss nur besser als "unknown"
# sein, nicht perfekt. ad_share = Anteil physischen Schadens (0..1).
_PRIOR_OVERRIDES: dict[str, float] = {
    # Bel'Veth: reine On-Hit-/Angriffstempo-Carry (physischer Schaden), aber
    # info gibt att=4/mag=7 -> 0.36 und liegt damit voellig falsch bei AP.
    "Belveth": 0.78,
    # Gwen: AP-Skirmisher, ihr Schaden (Q/W/Autos) ist magisch und AP-skaliert;
    # info att=7/mag=5 -> 0.58 stuft sie faelschlich AD-lastig ein. Pool-Champ.
    "Gwen": 0.35,
    # Kayle: On-Hit-Magieschaden-Carry (Autos + Ults magisch), info att=6/mag=7
    # -> 0.46 ist grenzwertig; real deutlich magielastiger.
    "Kayle": 0.35,
    # Data-Dragon-Datenluecke (Befund A, review-2026-07-15.md): Diese vier
    # Champions liefert Data Dragon mit attack=0/magic=0 aus -> Sonderfall 0.5,
    # obwohl ihr Schadensprofil eindeutig ist. Ohne Override laege z.B.
    # Seraphine neutral statt klar AP (und fiele in class_buckets nach ad_mage).
    # tests/test_champions.py waechtert, dass jede kuenftige Null-info-ID hier
    # eingetragen wird.
    "Akshan": 0.8,      # Marksman, ueberwiegend physisch (Passive-Bonus magisch)
    "Rell": 0.2,        # Tank-Support, Schaden fast komplett magisch
    "Seraphine": 0.1,   # AP-Mage/Support
    "Vex": 0.1,         # AP-Mage
}


@lru_cache(maxsize=4)
def damage_priors(version: str, cache_dir: Path) -> dict[str, float]:
    """Data-Dragon-ID -> ad_share (Anteil physischen Schadens) aus
    info.attack / (info.attack + info.magic). Sonderfall attack+magic == 0
    -> 0.5. Kuratierte Overrides haben Vorrang."""
    data = ddragon.sr_champion_data(version, cache_dir)
    priors: dict[str, float] = {}
    for info in data.values():
        cid = info["id"]
        stats = info.get("info", {})
        attack = stats.get("attack", 0)
        magic = stats.get("magic", 0)
        total = attack + magic
        priors[cid] = 0.5 if total == 0 else attack / total
    priors.update(_PRIOR_OVERRIDES)
    return priors


def damage_bucket(ad_shares) -> str | None:
    """'ad' | 'ap' | 'mixed' | None - Schadenstyp eines Teams aus dem
    UNGEWICHTETEN Mittel der Champion-ad_shares. Schwellen: Mittel >= 0.6 -> ad,
    <= 0.4 -> ap (ap_share >= 0.6), sonst mixed. Leere Liste -> None.

    Gemeinsame Definition fuer Aggregation (pipeline.aggregate) und Live-Abfrage
    (engine.recommend by_threat-Lookup), damit Train und Serve dieselben gelernten
    Zellen unter derselben Bucket-Definition zaehlen bzw. abfragen (Review G).
    Gegner ohne bekannten Prior werden vom Aufrufer weggelassen."""
    shares = list(ad_shares)
    if not shares:
        return None
    ad = sum(shares) / len(shares)
    if ad >= 0.6:
        return "ad"
    if ad <= 0.4:  # entspricht ap_share >= 0.6
        return "ap"
    return "mixed"


@lru_cache(maxsize=4)
def class_buckets(version: str, cache_dir: Path) -> dict[str, str]:
    """Data-Dragon-ID -> Klassen-Bucket. Der Bucket ist der primaere
    Data-Dragon-Tag (erstes Element von `tags`) kombiniert mit dem Schadens-Prior
    (ad/ap, Schwelle 0.5): z.B. 'ad_fighter', 'ap_mage', 'ad_marksman'. Tanks
    bekommen als Primaer-Tag nur 'tank' - der Schadenstyp ist fuer ihr
    Itemization-Profil zweitrangig. Champions ohne Tags werden ausgelassen.

    Gleiche Funktion fuer Pipeline (aggregate) und App (recommend-Fallback),
    damit Klassen-Aggregat und -Lookup exakt dieselbe Bucket-Definition nutzen."""
    data = ddragon.sr_champion_data(version, cache_dir)
    priors = damage_priors(version, cache_dir)
    buckets: dict[str, str] = {}
    for info in data.values():
        cid = info["id"]
        tags = info.get("tags") or []
        if not tags:
            continue
        primary = tags[0]
        if primary == "Tank":
            buckets[cid] = "tank"
            continue
        dmg = "ad" if priors.get(cid, 0.5) >= 0.5 else "ap"
        buckets[cid] = f"{dmg}_{primary.lower()}"
    return buckets


def resolve_id(champion_display_name: str) -> str | None:
    """Live-Anzeigename ('Bel'Veth', 'Wukong') -> Data-Dragon-ID ('Belveth',
    'MonkeyKing') oder None. Duenne Huelle um ddragon.resolve_name mit dem
    prozessweit gecachten Resolver-Kontext."""
    version, cache_dir = _resolver_ctx()
    return ddragon.resolve_name(version, cache_dir, champion_display_name)


def bucket_for_id(cid: str | None) -> str | None:
    """Data-Dragon-ID -> Klassen-Bucket ('ad_fighter') oder None. Locale-stabil,
    ohne Namensaufloesung (Fix 5.7)."""
    if not cid:
        return None
    version, cache_dir = _resolver_ctx()
    return class_buckets(version, cache_dir).get(cid)


@lru_cache(maxsize=1)
def _resolver_ctx() -> tuple:
    """Version + Cache-Pfad fuer die Namensaufloesung, einmal pro Prozess.
    Gleiches Muster wie app/knowledge._resolver_ctx."""
    from core.config import Config
    cache_dir = Config.load().cache_dir
    # Offline-tolerant: ohne Netz die neueste vollstaendig gecachte Version.
    return ddragon.latest_version_cached(cache_dir), cache_dir


def prior_for_id(cid: str | None) -> dict:
    """Data-Dragon-ID -> {'ad': x, 'ap': y} A-priori-Split. Locale-stabil, ohne
    Namensaufloesung (Fix 5.7). Unbekannt/None -> neutrales {'ad': 0.5, 'ap': 0.5}."""
    if not cid:
        return {"ad": 0.5, "ap": 0.5}
    version, cache_dir = _resolver_ctx()
    ad = damage_priors(version, cache_dir).get(cid, 0.5)
    return {"ad": round(ad, 2), "ap": round(1.0 - ad, 2)}


def ad_share_for_id(cid: str | None) -> float | None:
    """Data-Dragon-ID -> bekannter ad_share (0..1) oder None, wenn der Champion
    keinen Prior hat. Anders als prior_for_id (das fuer Unbekannte neutral 0.5
    liefert) unterscheidet das hier 'unbekannt' von 'neutral' - damit der
    by_threat-Lookup Gegner ohne Prior WEGLAESST, genau wie die Train-Seite
    (`_dmg_bucket` filtert `c in priors`). Review G."""
    if not cid:
        return None
    version, cache_dir = _resolver_ctx()
    return damage_priors(version, cache_dir).get(cid)


def cc_per_min_for_id(cid: str | None) -> float | None:
    """Data-Dragon-ID -> CC-Prior (cc_per_min, CC-Sekunden je Minute) oder None,
    wenn der Champion keinen Prior hat. Symmetrisch zu ad_share_for_id, aber die
    Quelle ist die aggregierte Wissensbasis (cc_priors in builds.yaml), nicht die
    Data-Dragon-Statik - CC laesst sich nur empirisch messen. Lazy-Import von
    engine.knowledge, um Modul-Importzyklen zu vermeiden."""
    from . import knowledge
    return knowledge.cc_prior_for_id(cid)
