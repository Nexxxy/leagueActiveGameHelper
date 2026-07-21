# LoL Live-Helper (Release)

Item-Empfehlungen fuer das laufende Spiel, mit vortrainierter Wissensbasis.

## Voraussetzung
[uv](https://docs.astral.sh/uv/): `winget install astral-sh.uv`

## Starten (Git Bash)
```bash
./start.sh          # echtes Spiel (liest die LoL Live Client Data API)
./start.sh --demo   # Demo-Modus ohne laufendes Spiel
./kill.sh           # Server stoppen
```
Dann im Browser: http://127.0.0.1:8000

Beim ersten Start legt uv automatisch das venv an und die App laedt die
statischen Spieldaten (Data Dragon) nach - einmalig Internet noetig.
