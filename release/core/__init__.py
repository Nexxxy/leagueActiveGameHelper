"""Shared Kernel: Infrastruktur + Basiskonfiguration.

Unterste Schicht der Architektur. `core` kennt KEIN anderes Projektpaket -
weder `engine`, noch `pipeline`, noch `app`. Erlaubt sind hier ausschliesslich
Standardbibliothek und Fremdpakete (yaml, requests).

Inhalt:
- `config`   Config-Dataclass, ROOT, VALID_ROLES, normalize_role, SEED_LADDER
- `ddragon`  Data-Dragon-Statik (Versionen, Item-/Champion-Daten)
- `riot_api` RiotClient (Rate-Limits, Round-Robin, Retry)
- `cacheio`  JSON-Cache-I/O, Frische-Pruefung, Skip-Marker
- `stats`    Statistische Hilfsfunktionen (Shrinkage)

Alle anderen Pakete duerfen hierauf zugreifen (`engine`, `pipeline`, `app`);
die Richtung ist per `tests/test_architecture.py` festgenagelt.
"""
