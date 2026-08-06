# League Active Game Helper

League Active Game Helper is a post-game analysis companion that helps players learn from their matches and make smarter itemization decisions over time. After a game, it reviews the items every player built, reconstructs the enemy team's threat profile, and shows how your itemization choices matched up against what the game actually demanded.

The tool turns each match into a learning opportunity. It highlights the enemy power spikes you faced, points out where Anti-Heal coverage against sustain champions would have helped, and shows how your build could have adapted to gold leads or deficits. Every insight comes with an honest confidence level, so players always know how much data stands behind each takeaway — no false certainty, just grounded feedback.

Recommendations are powered by a self-aggregated high-elo knowledge base, built by crawling ranked matches through the Riot Web API and distilling the typical builds for each champion-and-role combination. A class-based fallback fills the gaps for off-meta picks, so even unconventional builds get sensible, data-backed analysis.

How it helps players: itemization is one of the hardest skills to review after a game, because build sites can't see the specific match you played. This tool closes that gap — giving players opponent-aware, context-rich feedback in the browser so they can understand their buying decisions, recognize recurring patterns, and steadily improve their itemization for future games.

While a game is running, the same knowledge base drives live item recommendations for your champion, based on the items your opponents have actually bought.

## Getting Started

### 1. Install uv

The only prerequisite is [uv](https://docs.astral.sh/uv/), which manages Python and all
dependencies for you — no manual virtualenv, no `pip install`:

```powershell
winget install astral-sh.uv
```

macOS/Linux (or if you prefer the installer script):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

uv fetches a matching Python (3.11+) itself, so nothing else needs to be installed.

### 2. Get the helper

Either clone this repository:

```bash
git clone https://github.com/Nexxxy/leagueActiveGameHelper.git
cd leagueActiveGameHelper/release
```

…or download `release-<version>.zip` from the [Releases](https://github.com/Nexxxy/leagueActiveGameHelper/releases)
page and unpack it. Everything below is run from the folder that contains `start.sh` /
`start.ps1`.

### 3. Start the server

PowerShell (Windows):

```powershell
.\start.ps1              # live mode: reads the local LoL Live Client Data API (port 2999)
.\start.ps1 --demo       # demo mode: synthetic game state, no League client needed
.\start.ps1 --port 8080  # use a different port (default 8000)
.\kill.ps1               # stop the server
```

Git Bash / macOS / Linux:

```bash
./start.sh
./start.sh --demo
./start.sh --port 8080
./kill.sh
```

Then open **http://127.0.0.1:8000** in your browser.

The start scripts run `uv sync` for you: on first launch uv creates the virtual
environment and installs the dependencies, which takes a moment. Afterwards startup is
instant. The app also pulls the static Data Dragon game data on first run, so one-time
internet access is required — no Riot API key is needed for any of this.

Prefer to run it yourself without the scripts?

```bash
uv sync
uv run python -m app.server --demo
```

### 4. Optional configuration

`config.yml` sits next to the start scripts and works out of the box. Two settings are
worth knowing:

- `app.refresh_seconds` — how often the browser view refreshes (in seconds).
- `riot.api_key` plus `app.me: Name#Tag` — your own Riot API key and Riot ID. Entirely
  optional; see the post-game report below for what they add.

## Post-Game Report

After every finished Summoner's Rift game, the helper automatically writes an HTML
report to `postgame/live_<timestamp>.html` — **no API key required**. It covers gold,
CS, vision and KDA over time, a scoreboard, objectives, death and teamfight timings,
plus a build and team-composition diagnosis.

**Important:** the server has to be running *before* the game starts. If you launch it
later (from roughly minute 2 onwards), the report will be missing part of the match
history.

If you add your own Riot API key (`riot.api_key`) and your Riot ID (`app.me: Name#Tag`)
to `config.yml`, the report is automatically upgraded after the game with damage charts
and an impact score. Without a key the report stays fully usable — a disclaimer inside
it explains which damage-based sections are missing and why.
