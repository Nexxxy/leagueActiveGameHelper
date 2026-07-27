"""Basis-Info-Manifest (Schicht 0): welche Referenzdaten liegen vor, woher
stammen sie, wie frisch sind sie.

"Schicht 0" sind die Daten, auf denen alles andere aufsetzt:

1. Data-Dragon-Statics je Version (`<cache_dir>/static/item_<ver>.json` und
   `champion_<ver>.json`) - Item-Namen/-Preise/-Tags und die Champion-Liste.
2. Die generierte Wissensbasis (`<out_dir>/<patch>/builds.yaml`) samt ihrer
   Provenienz (`inherited_from`, wenn Eintraege vom Vor-Patch geerbt sind) und
   den kuratierten Overrides.

Das Manifest ist REINE DIAGNOSE: es liest nur, was da ist, und stoesst
niemals einen Download oder Crawl an. Die Live-Version wird zwar uebers Netz
versucht, ein Fehlschlag ist aber kein Fehler - dann bleibt `live_version`
None und die Stale-Flags bleiben None ("nicht pruefbar" statt "aktuell").

Liegt in `engine/`, damit sowohl `app/` (Server-Hinweis beim Start) als auch
`pipeline/` (`pipeline status`) es nutzen duerfen.
"""

from pathlib import Path

from core import ddragon
from . import knowledge


def build_manifest(cfg) -> dict:
    """Zustand der Referenzdaten als dict (siehe Modul-Docstring).

    Struktur:
      statics:      dir, versions (vollstaendige Paare, aufsteigend), latest
      knowledge:    patch, inherited_from, path, mtime, champions, overrides
      live_version: Data-Dragon-Live-Version oder None (offline)
      statics_stale/kb_stale: True/False gegen die Live-Version,
                    None wenn offline (dann ist Frische schlicht nicht pruefbar)
    """
    cache_dir = Path(cfg.cache_dir)
    versions = ddragon.cached_versions(cache_dir)

    kb = knowledge.load()
    kb_patch = kb.get("patch") or ""
    builds_path = knowledge.source_path()

    try:
        live_version = ddragon.latest_version()
    except Exception:   # noqa: BLE001 - offline ist ein gueltiger Zustand
        live_version = None

    statics_stale = kb_stale = None
    if live_version:
        statics_stale = (not versions) or versions[-1] != live_version
        kb_stale = kb_patch != ddragon.patch_of(live_version)

    return {
        "statics": {
            "dir": str(cache_dir / "static"),
            "versions": versions,
            "latest": versions[-1] if versions else None,
        },
        "knowledge": {
            "patch": kb_patch,
            "inherited_from": kb.get("inherited_from"),
            "path": str(builds_path) if builds_path else None,
            "mtime": builds_path.stat().st_mtime if builds_path else None,
            "champions": len(kb.get("champions", {})),
            "overrides": knowledge.overrides_path().exists(),
        },
        "live_version": live_version,
        "statics_stale": statics_stale,
        "kb_stale": kb_stale,
    }


def stale_hint(manifest: dict) -> str | None:
    """Eine Zeile Klartext, wenn Statics oder Wissensbasis hinter der
    Live-Version zurueckliegen - sonst None. Bewusst nur ein HINWEIS: was
    nachgeladen/gecrawlt wird, entscheidet der Nutzer."""
    veraltet = []
    if manifest.get("statics_stale"):
        veraltet.append("Data-Dragon-Statics")
    if manifest.get("kb_stale"):
        veraltet.append("Wissensbasis")
    if not veraltet:
        return None
    return (f"{' und '.join(veraltet)} sind aelter als der Live-Patch "
            f"{manifest.get('live_version')} - 'python -m pipeline focus' bzw. "
            f"'python -m pipeline aggregate' bringt sie auf Stand.")
