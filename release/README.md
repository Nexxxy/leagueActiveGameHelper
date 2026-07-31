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

## Starten (PowerShell)
```powershell
.\start.ps1          # echtes Spiel (liest die LoL Live Client Data API)
.\start.ps1 --demo   # Demo-Modus ohne laufendes Spiel
.\kill.ps1           # Server stoppen
```
Dann im Browser: http://127.0.0.1:8000

Beim ersten Start legt uv automatisch das venv an und die App laedt die
statischen Spieldaten (Data Dragon) nach - einmalig Internet noetig.

## Post-Game-Report
Nach jedem beendeten Summoner's-Rift-Spiel erzeugt der Helper automatisch einen
HTML-Report unter `postgame/live_<zeitstempel>.html` - komplett ohne API-Key.
Er zeigt Gold-/CS-/Vision-/KDA-Verlaeufe, Scoreboard, Objectives, Todes-/
Teamfight-Timing, Build- und Comp-Diagnose.

Wichtig: Der Server muss VOR Spielbeginn laufen. Wird er erst spaeter (ab ~Minute
2) gestartet, fehlt die volle Statistik.

Optional: Traegst du in `config.yml` einen eigenen Riot-API-Key (`riot.api_key`)
plus deine Riot-ID (`app.me: Name#Tag`) ein, wird der Report nach dem Spiel
automatisch um Schaden-Diagramme und einen Impact-Score aufgewertet. Ohne Key
ist er voll nutzbar; ein Disclaimer erklaert dann die fehlenden Schaden-Teile.
