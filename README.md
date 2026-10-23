# League Active Game Helper

League Active Game Helper is a post-game analysis companion that helps players learn from their matches and make smarter itemization decisions over time. After a game, it reviews the items every player built, reconstructs the enemy team's threat profile, and shows how your itemization choices matched up against what the game actually demanded.

The tool turns each match into a learning opportunity. It highlights the enemy power spikes you faced, points out where Anti-Heal coverage against sustain champions would have helped, and shows how your build could have adapted to gold leads or deficits. Every insight comes with an honest confidence level, so players always know how much data stands behind each takeaway — no false certainty, just grounded feedback.

Recommendations are powered by a self-aggregated high-elo knowledge base, built by crawling ranked matches through the Riot Web API and distilling the typical builds for each champion-and-role combination. A class-based fallback fills the gaps for off-meta picks, so even unconventional builds get sensible, data-backed analysis.

How it helps players: itemization is one of the hardest skills to review after a game, because build sites can't see the specific match you played. This tool closes that gap — giving players opponent-aware, context-rich feedback in the browser so they can understand their buying decisions, recognize recurring patterns, and steadily improve their itemization for future games.

While a game is running, the same knowledge base drives live item recommendations for your champion, based on the items your opponents have actually bought.

## Getting Started

The helper runs on **Windows only** (developed and tested on Windows 11). The `.sh` scripts are for Git Bash;
everything else works from Explorer, `cmd` or PowerShell. No Riot API key is needed.
Script messages are in German.

### 1. Get the helper

Download `release-<version>.zip` from the [Releases](https://github.com/Nexxxy/leagueActiveGameHelper/releases)
page. **Before unpacking**, right-click the zip → *Properties* → tick **Unblock** and
click OK — this removes the "downloaded from the internet" mark. If you skip it,
SmartScreen may warn when you double-click a `.bat` file: click *More info* →
*Run anyway*.

Or clone this repository:

```bash
git clone https://github.com/Nexxxy/leagueActiveGameHelper.git
cd leagueActiveGameHelper/release
```

Everything below is run from the folder that contains `install.bat` and `start.bat`.

### 2. Install (one time)

Double-click `install.bat`, or run one of:

```powershell
.\install.bat        # cmd / PowerShell
```

```bash
bash install.sh      # Git Bash
```

What it does:

- Looks for [uv](https://docs.astral.sh/uv/) (Astral's Python manager) on your `PATH`
  and in its usual install locations. Only if none is found, it downloads uv into the
  subfolder `astral-uv/` — no admin rights, no `PATH` changes.
- Runs `uv sync`, which sets up Python 3.13 and all dependencies. Python and the uv
  cache also live under `astral-uv/` (unless a matching Python is already installed),
  the virtual environment under `.venv/`.
- Everything stays inside this folder: to uninstall, delete it (and undo the optional
  execution-policy change below, if you made it). After installing, don't move the
  folder — the virtual environment points to its current location.

Options (`install.bat` passes them on to `install.ps1`):

| `install.bat` / `.\install.ps1` | `bash install.sh` | Effect |
|---|---|---|
| `-Check` | `--check` | only report whether/where uv is found (no download, no changes) |
| `-Force` | `--force` | download uv into `astral-uv/` again (keeps Python and cache) |
| `-NoSync` | `--no-sync` | skip `uv sync` (it then runs on the first start) |
| `-NoPrompt` | — | don't ask about the execution policy |
| `-Help` | `-h`, `--help` | show help |

Set the environment variable `UV_VERSION` (e.g. `0.13.5`) to pin the uv version;
the default is the latest.

**PowerShell execution policy:** `start.bat` and `kill.bat` always work — they bypass the
policy for that one call only. To run `.\start.ps1` directly, the installer offers
(default: no) to set the policy to `RemoteSigned` for your user account; it prints the
command to undo it. If the policy is enforced by Group Policy (e.g. on company PCs),
neither the installer nor `start.bat` can get around it — use `./start.sh` under Git
Bash or ask your administrator.

### 3. Start the server

| | Windows (Explorer / `cmd`) | PowerShell | Git Bash |
|---|---|---|---|
| live mode | `start.bat` | `.\start.ps1` | `./start.sh` |
| demo mode | `start.bat --demo` | `.\start.ps1 --demo` | `./start.sh --demo` |
| other port | `start.bat --port 8080` | `.\start.ps1 --port 8080` | `./start.sh --port 8080` |
| stop | `kill.bat` | `.\kill.ps1` | `./kill.sh` |

- **Live mode** reads the local League of Legends Live Client Data API (port 2999)
  while a game is running. **Demo mode** uses a synthetic game state — no League client
  needed.
- Options can be combined (`--demo --port 8080`); the default port is 8000.
- In a freshly unpacked zip the `.sh` files may not be executable — use
  `bash start.sh` / `bash kill.sh` instead.

The server runs in the background and your browser opens automatically. If it doesn't,
open **http://127.0.0.1:8000** (or the port you chose). Logs go to `server.log`.

Each start runs `uv sync` first, which is instant once the install is done. On start the
app downloads missing item icons from Riot's Data Dragon in the background, so internet
access is needed once; without it, startup is not blocked.

### 4. Optional configuration

`config.yml` sits next to the start scripts and works out of the box. Two settings are
worth knowing:

- `app.refresh_seconds` — how often the browser view refreshes (in seconds).
- `riot.api_key` plus `app.me: Name#Tag` — your own Riot API key and Riot ID. Entirely
  optional; see the post-game report below for what they add.

The server reads `config.yml` only when it starts — after a change, stop it and start it
again.

### 5. Troubleshooting

- **`uv sync` fails with a source build or "Microsoft Visual C++"** — no prebuilt
  packages (wheels) exist for the Python version uv picked. The release pins Python 3.13
  in `.python-version`; don't change or delete that file.
- **"Port 8000 ist belegt" (port in use)** — an old server is still running. Stop it with
  `kill.bat` / `.\kill.ps1` / `./kill.sh`, or start on another port with `--port`.
- **"uv wurde nicht gefunden" (uv not found)** — run the install step first.
- **Server doesn't come up** — the last lines of `server.log` are printed; the full log is
  in `server.log`.

*Manual alternative:* if you prefer your own uv (e.g. `winget install astral-sh.uv`),
skip the scripts and run `uv sync` followed by `uv run python -m app.server --demo`
(without `--demo` for live mode). The server then runs in the foreground, and a
downloaded Python plus the uv cache go to uv's default locations in your user profile
instead of `astral-uv/`.

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
