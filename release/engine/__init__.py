"""Domaenen-Logik: Empfehlungs-Engine + Domaenen-Wissen.

Mittlere Schicht: `engine` darf ausschliesslich `core` (und Standard-/Fremd-
bibliotheken) importieren - NIE `pipeline` oder `app`. Dadurch ist dieselbe
Engine sowohl vom Crawler-System (`pipeline`, z.B. Backtest/Aggregation) als
auch vom Spieler-System (`app`, Live-Server + Post-Game-Report) nutzbar, ohne
dass eines der beiden das andere nachzieht.

Inhalt:
- `items`          Item-Statik, Klassifikation, Item-Mengen
- `champions`      Champion-Statik, Schadensprofile, Identitaet
- `knowledge`      Zugriff auf die generierte Wissensbasis (builds.yaml)
- `profiling`      Spieler-/Gegner-Profile aus Live- bzw. Match-Daten
- `recommend`      Empfehlungs-Engine (Fassade ueber die rec_*-Schichten)
- `rec_stance` / `rec_archetype` / `rec_antiheal` / `rec_explain` /
  `rec_partner`    Einzelschichten der Engine
- `replay_profile` Adapter fuer Engine-Replays (Backtest + Post-Game-Report)

Die Richtung ist per `tests/test_architecture.py` festgenagelt.
"""
