"""Archetyp-Wahl: waehlt aus mehreren Meta-Build-Varianten den Pfad, auf den
der Spieler laut seinen bereits gekauften Items committet ist.

Aus recommend.py ausgelagert (Struktur-Review 2026-07-17, Befund S2).
"""

from . import items


# Offense/Defense fuer den Archetyp-Tilt - aus der EINEN Taxonomie (Fix 5.6).
_OFFENSE_TAGS = items.AD_TAGS | items.AP_TAGS
_DEFENSE_TAGS = items.DEF_TAGS


def _archetype_tilt(build: dict) -> str:
    """'aggressive' | 'defensive' | 'neutral' aus den Core-Item-Tags. Oft sind
    die zwei Meta-Varianten genau die aggressive vs. defensive Auslegung
    (Lethality/Snowball vs. Bruiser/Survivability)."""
    off = dfn = 0
    for it in build.get("core", []):
        tags = items.tags_of(it["item"])
        off += len(tags & _OFFENSE_TAGS)
        dfn += len(tags & _DEFENSE_TAGS)
    if dfn > off:
        return "defensive"
    if off > dfn:
        return "aggressive"
    return "neutral"


def _select_archetype(builds: list[dict], owned_names: set[str],
                      stance: str | None = None) -> tuple[dict, str]:
    """Waehlt den Build-Pfad, auf dem der Spieler laut seinen bereits gekauften
    Items committet ist. Kern des Anwendungsfalls 'passt zu allem, was ich schon
    gebaut habe': Aus mehreren Meta-Archetypen wird der genommen, dessen
    Core-Items der Spieler am staerksten besitzt.

    Guardrail: Frueh (noch keine unterscheidenden Items) ist die Zuordnung
    mehrdeutig. Dann entscheidet die Stance: bei aggressiv/defensiv wird der
    entsprechend ausgelegte Archetyp bevorzugt (B1-Flag), sonst der haeufigste
    (pick_share). Rueckgabe: (archetyp, begruendung)."""
    if not builds:
        return {}, ""
    if len(builds) == 1:
        return builds[0], ""

    def commit(b: dict) -> int:
        return len({i["item"] for i in b.get("core", [])} & owned_names)

    ranked = sorted(builds, key=lambda b: (commit(b), b.get("pick_share", 0)),
                    reverse=True)
    c0 = commit(ranked[0])
    if c0 > commit(ranked[1]):
        return ranked[0], f"passt zu deinen bisherigen Items ({ranked[0]['name']}-Build)"

    # Voll mehrdeutig - der Spieler besitzt KEIN unterscheidendes Core-Item eines
    # der Archetypen (Befund D2): NICHT vorzeitig auf einen Build committen. Frueher
    # waehlte hier Stance-Tilt/pick_share den haeufigeren Build und verdraengte damit
    # das Kern-Item des anderen Archetyps aus den Top-3. Konkret warf der Trinity-
    # Commit auf dem ersten Kauf bei Shyvana Kraken Slayer aus der Kandidatenliste,
    # obwohl der Spieler genau den baute. Leer zurueckgeben -> Fallback auf den
    # globalen core/situational (beide Fruehitems bleiben Kandidaten). Offline-
    # Backtest (Holdout, Patch 16.13): Shyvana Hit@3 62,1% -> 70,0% (+7,9pp),
    # Briar unveraendert (53,5% -> 53,6%), Hit@1 beider unveraendert.
    if c0 == 0:
        return {}, ""

    # Teil-mehrdeutig (gleiche Commit-Zahl > 0): Stance darf den Archetyp waehlen,
    # wenn er zur Auslegung passt.
    if stance in ("aggressive", "defensive"):
        match = next((b for b in ranked if _archetype_tilt(b) == stance), None)
        if match:
            wording = "aggressiver" if stance == "aggressive" else "defensiver"
            return match, f"{wording} Build ({match['name']}) - passt zu deiner Lage"
    dominant = max(builds, key=lambda b: b.get("pick_share", 0))
    return dominant, f"haeufigster Build ({dominant['name']}), noch keine Festlegung erkennbar"
