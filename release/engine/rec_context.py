"""Lauf-Kontext der Empfehlungs-Engine: die `_RecContext`-Dataclass plus die
generischen Helfer, die mehrere Schichten brauchen (Schadenstyp-Bucket des
Gegnerteams, Spike-Warnungen, Konfidenz-Stufe, Shrinkage, Redundanz-Check,
Synergie-Bonus).

Aus recommend.py ausgelagert (Modul-Split, T1). `_build_context` bleibt bewusst
im Orchestrator recommend.py - es verdrahtet Helfer aus fast allen
Feature-Modulen.
"""

from dataclasses import dataclass, field

from core import stats

from . import champions, items
from .rec_weights import CONF_RICH_MIN, SHRINK_K, Weights


def _enemy_damage_bucket(enemy_profiles: list[dict]) -> str | None:
    """'ad' | 'ap' | None - Schadenstyp-Bucket des Gegnerteams in der
    TRAIN-Definition (ungewichtetes Mittel der Champion-Priors, Gegner ohne
    Prior werden ausgelassen; s. pipeline/aggregate.py `_dmg_bucket`).

    Bewusst NICHT der threat-gewichtete `team_damage_split`: die by_threat-/
    boots_by_threat-Zellen wurden unter dieser Definition GEZAEHLT, und Train und
    Serve muessen denselben Bucket bilden (Review-Befund G). `team_damage_split`
    bleibt fuer Boots-Regeltexte, `_is_defensive` und die UI zustaendig."""
    shares = [s for s in (champions.ad_share_for_id(p.get("champion_id"))
                          for p in enemy_profiles) if s is not None]
    bucket = champions.damage_bucket(shares)
    return bucket if bucket in ("ad", "ap") else None


def _spike_warnings(enemy_profiles: list[dict], my_completed: int) -> list[dict]:
    """Awareness-Regel ohne jede Champion-Statistik (Review Befund 4.2): hat ein
    Gegner >= 1 fertiges Item mehr als der Spieler, eine Spike-Warnung erzeugen
    (>= 2 dringlicher). Reine Information - KEIN Eingriff ins Scoring/die Stance."""
    out = []
    for e in enemy_profiles:
        ec = e.get("completed_items")
        if ec is None:
            continue
        ahead = ec - my_completed
        if ahead < 1:
            continue
        plural = "Items" if ahead > 1 else "Item"
        out.append({
            "name": e.get("name"),
            "enemy_items": ec,
            "my_items": my_completed,
            "ahead_by": ahead,
            "urgency": "high" if ahead >= 2 else "medium",
            "message": f"{e.get('name')} ist dir {ahead} {plural} voraus",
        })
    out.sort(key=lambda w: -w["ahead_by"])
    # Deckel (Review Befund H.1): liegt man hinten, wuerden sonst alle fuenf
    # Gegner gleichzeitig warnen (Alarm-Tapete). Nur die 2 groessten Vorspruenge.
    return out[:2]


def confidence_tier(kb: dict) -> str:
    """'rich' | 'basic' | 'thin' aus der Datenlage der Kombi.
    thin  = gar kein KB-Eintrag (unter cfg.min_games kein Build-Wissen),
    basic = Eintrag vorhanden, aber unter CONF_RICH_MIN (situative Schichten
            zu duenn - nur der Core-Pfad ist belastbar),
    rich  = >= CONF_RICH_MIN Spiele, volles Verhalten."""
    if not kb:
        return "thin"
    return "rich" if kb.get("games", 0) >= CONF_RICH_MIN else "basic"


def _shrunk(win_rate: float, n: int, base: float, k: float = SHRINK_K) -> float:
    """Geschrumpfte Win-Rate Richtung Basisrate. Duenner Wrapper um
    core.stats.shrunk (Befund D3) mit SHRINK_K als Item-Prior-Default."""
    return stats.shrunk(win_rate, n, base, k)


# Reine Sustain-Stats, bei denen ein zweites fertiges Item kaum Mehrwert bringt
# (anders als Ruestung/MR/HP, die Tanks bewusst stapeln - die NICHT abwerten).
_REDUNDANT_TAGS = {"LifeSteal", "SpellVamp"}


def _redundant_stack(name: str, owned_names: set[str]) -> bool:
    """True, wenn der Kandidat einen Sustain-Stat traegt, den ein bereits
    besessenes FERTIGES Item schon liefert (zweiter Lifesteal lohnt selten)."""
    cand = items.tags_of(name) & _REDUNDANT_TAGS
    if not cand:
        return False
    for owned in owned_names:
        oe = items.by_name().get(owned)
        if oe and not oe[1].get("into") and cand & set(oe[1].get("tags", [])):
            return True
    return False


def _synergy_boost(name: str, owned_ids: list[int], factor: float = 0.3) -> float:
    """0..factor - je weiter der Spieler ueber vorhandene Komponenten schon in
    dieses Item investiert hat, desto hoeher wird es priorisiert (nicht nur
    billiger). Belohnt 'fertigbauen, was ich angefangen habe'."""
    if not owned_ids:
        return 0.0
    entry = items.by_name().get(name)
    if not entry:
        return 0.0
    total = entry[1].get("gold", {}).get("total", 0) or 1
    disc = items.build_discount(name, owned_ids)
    return factor * min(disc / total, 1.0)


@dataclass
class _RecContext:
    """Langlebige Zwischenergebnisse einer recommend()-Auswertung (Struktur-
    Review 2026-07-17 T3, Befund S1). Buendelt Kontext- und Kandidaten-Daten, die
    ueber mehrere Phasen (Core-Pick, Boots, konditionale Schichten, Scoring,
    Result-Assembly) hinweg leben, statt sie als lange Argumentketten
    durchzureichen. `_build_context` baut das Objekt auf; `_conditional_layers`
    befuellt die konditionalen/Klassen-Felder nach."""
    # Rohe Aufruf-Parameter, die spaetere Phasen noch brauchen
    champion: str
    used_role: str | None
    role: str | None
    cid: str
    owned_names: set
    owned_ids: list
    enemy_profiles: list
    ally_items: set
    game_time: float
    current_gold: int | None
    weights: Weights
    # Botlane-Partner-Kontext (research_bot_sup_mates.md 9.40): das Profil-Dict
    # des eigenen BOTTOM-Partners plus Schluessel `partner_class`, oder None fuer
    # alle Nicht-UTILITY-Faelle. In dieser Tranche nur abgelegt (Durchreichung);
    # der auswertende Layer folgt in T4.
    bot_partner: dict | None
    # Wissensbasis + abgeleitete Kontext-Kennzahlen (Phase 1)
    kb: dict
    top: dict | None
    split: dict
    enemy_cc_score: float
    fielded_lead: int | None
    earned_lead: int | None
    gold_state: str | None
    stance: str
    stance_reason: str
    build: dict
    build_reason: str
    core_source: list
    situational_source: list
    confidence: str
    has_boots: bool
    boots_options: list
    # Uebergangs-Bigramm (next_after, T1/T2): {<Vorgaenger>: [{item, count,
    # win_rate}]} des Champion+Rollen-Eintrags. Leer bei alter KB ohne Block.
    next_after: dict = field(default_factory=dict)
    # Ausgewertetes Bigramm (einmal pro Lauf gebaut, s. _next_after_model) plus
    # die FERTIGEN Besitz-Items als Bezugsmenge O des Lifts.
    na_cond: dict = field(default_factory=dict)
    na_marginal: dict = field(default_factory=dict)
    na_owned: list = field(default_factory=list)
    # Restpfad-Neubewertung (V2-05): Slot-Support P(Slot|Item), aktueller
    # Kaufslot, gelernte Exklusiv-Paare. `path_scores`/`path_block` werden von
    # _core_pick/_score_situationals BEFUELLT (Pool-Score je Kandidat bzw. Namen
    # ohne Support am aktuellen Slot) und in _assemble_result gelesen.
    slot_dist: dict = field(default_factory=dict)
    slot_horizon: int = 0
    cur_slot: int = 1
    exclusive: list = field(default_factory=list)
    path_scores: dict = field(default_factory=dict)
    path_block: frozenset = frozenset()
    # Todes-Signal aus dem Live-Kill-Feed (V2-08, `engine/rec_deaths.py`):
    # {deaths, champion, champion_deaths, damage_type, trigger, reason} oder
    # None. Default None (Layer stumm) - so bleiben alte Aufrufer/Tests, die den
    # Kontext direkt bauen, unveraendert gueltig, und Backtest/Demo/alte Dumps
    # verhalten sich exakt wie vor V2-08.
    death_signal: dict | None = None
    # Konditionale Schichten (by_threat/by_state) - von _conditional_layers gefuellt
    enemy_bucket: str | None = None
    bt: dict | None = None
    threat_items: dict = field(default_factory=dict)
    threat_base: float | None = None
    state_items: dict = field(default_factory=dict)
    state_base: float | None = None
    # Partner-konditioniert (by_partner, Phase 2) - von _conditional_layers gefuellt
    partner_items: dict = field(default_factory=dict)
    partner_base: float | None = None
    partner_bucket: str | None = None
    # Klassen-Fallback - von _conditional_layers gefuellt
    lookup_role: str = ""
    class_bucket: str | None = None
    class_situational: list = field(default_factory=list)
    class_boots: list = field(default_factory=list)
    class_games: int = 0
    class_label: str = ""
    champ_pool: set = field(default_factory=set)


def _tag_role(ctx: _RecContext) -> str | None:
    """Rolle fuer das Support-Framing der Tags/Erklaertexte (T4b): nur "UTILITY"
    aktiviert die Support-Kategorien, sonst None (byte-identisches Verhalten)."""
    return ctx.role if ctx.role == "UTILITY" else None
