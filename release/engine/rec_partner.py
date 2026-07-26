"""Partner-Klassifikator fuer die Botlane (Achse "Partner-Kontext", 9.40).

Auf der Botlane haengt die Item-Wahl eines Supports stark am eigenen
BOTTOM-Partner: ein AD-Carry und ein AP-Carry verlangen unterschiedliche
Ally-Buff-Items. Empirisch (Patch 16.13+16.14, Diamond-Index) kippt die
Pickrate der reinen Ally-Buff-Items rein am Schadenstyp des Partners:
Ardent Censer 7,3 % (AD-Partner) -> 1,4 % (AP-Partner), Staff of Flowing
Water 1,1 % -> 4,6 %. Details in docu/research_bot_sup_mates.md (Abschnitt 9.40).

Dieses Modul liefert nur die Klassifikation; der Empfehlungs-Layer, der sie
auswertet, folgt in einer spaeteren Tranche (T4).
"""

from . import champions


def classify_partner(champion_id: str,
                     partner_profile: dict | None = None) -> str:
    """Data-Dragon-ID des Bot-Partners -> "ad_carry" | "ap_carry" | "unknown".

    Zuerst entscheidet der eindeutige Live-Build (`build_profile`), dann der
    Champion-Schadens-Prior als Fallback. Ein committeter Build (>=1500 Gold +
    Schwellen in profiling.build_profile) schlaegt den nominellen Prior -
    konsistent zur `_damage_split`-Philosophie beim Gegner-Profil (behebt z.B.
    voll-AP-Kog'Maw, dessen ad_share=0.615 knapp ueber der Prior-Schwelle liegt):
    1. build_profile "burst_ap" -> ap_carry,
    2. build_profile "burst_ad"/"crit_dps" -> ad_carry,
    3. sonst (kein/ambivalentes Profil): Prior ad_share >= 0.6 -> ad_carry,
       <= 0.4 -> ap_carry, dazwischen "unknown".

    Kein Netz-Call zur Laufzeit: fehlt der Data-Dragon-Cache oder schlaegt der
    Prior-Lookup fehl, wird defensiv "unknown" geliefert (Offline-Guard analog
    zum uebrigen app-Code)."""
    if not champion_id:
        return "unknown"
    # 1./2. Eindeutiger Live-Build schlaegt den Prior.
    build = (partner_profile or {}).get("build_profile")
    if build == "burst_ap":
        return "ap_carry"
    if build in ("burst_ad", "crit_dps"):
        return "ad_carry"
    # 3. Prior als Fallback (unknown/hybrid/tank/kein Profil).
    try:
        ad_share = champions.ad_share_for_id(champion_id)
    except Exception:
        return "unknown"
    if ad_share is None:
        return "unknown"
    if ad_share >= 0.6:
        return "ad_carry"
    if ad_share <= 0.4:
        return "ap_carry"
    return "unknown"
