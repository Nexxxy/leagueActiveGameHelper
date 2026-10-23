[CmdletBinding(PositionalBinding = $false)]
param(
    # Intern: start.ps1 ruft sich selbst versteckt mit -ServerChild auf
    # (base64-kodiertes JSON der Server-Argumente). Nicht von Hand setzen.
    [string] $ServerChild,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)

# Startet den Live-Helper-Server im Hintergrund.
# Alle Optionen werden durchgereicht, z.B.:
#   .\start.ps1 --demo
#   .\start.ps1 --demo --port 8080

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

# Kindprozess-Modus: nur den Server starten, keine Elternlogik. PATH und
# UV_PYTHON_INSTALL_DIR/UV_CACHE_DIR erbt der Kindprozess vom Elternprozess
# (dort aufgeloest und gesetzt), "uv" wird deshalb blank aufgerufen.
# Bewusst per -File statt als base64-kodierter Befehl: Microsoft Defender
# meldet einen versteckten PowerShell-Start mit kodiertem Befehl unter
# Windows PowerShell 5.1 als Trojan:Win32/Commando.A!ml und blockiert ihn.
if ($PSBoundParameters.ContainsKey("ServerChild")) {
    $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ServerChild))
    $appArgs = @()
    if (-not [string]::IsNullOrWhiteSpace($json)) {
        $parsedArgs = ConvertFrom-Json -InputObject $json
        if ($null -ne $parsedArgs) {
            $appArgs = @($parsedArgs)
        }
    }
    # Stderr des Servers landet im Log; unter Windows PowerShell 5.1 wird es
    # dabei zu Fehlerdatensaetzen, die mit "Stop" den Kindprozess abbraechen.
    $ErrorActionPreference = "Continue"
    & uv run python -u -m app.server @appArgs *> server.log
    exit $LASTEXITCODE
}

if ($null -eq $RemainingArgs) {
    $RemainingArgs = [string[]]@()
} else {
    $RemainingArgs = [string[]]@($RemainingArgs)
}

function Get-PidsOnPort {
    param([int] $Port)

    try {
        return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Sort-Object -Unique)
    } catch {
        $pids = New-Object System.Collections.Generic.List[int]

        netstat -ano 2>$null | ForEach-Object {
            $parts = @(($_ -split '\s+') | Where-Object { $_ })
            if ($parts.Count -ge 5 -and $parts[0] -eq "TCP") {
                $local = $parts[1]
                $remote = $parts[2]
                $pidText = $parts[4]

                if ($local -match ":$Port$" -and
                    ($remote -eq "0.0.0.0:0" -or $remote -eq "[::]:0") -and
                    $pidText -match '^\d+$') {
                    $pids.Add([int] $pidText)
                }
            }
        }

        return @($pids | Sort-Object -Unique)
    }
}

$port = 8000
for ($i = 0; $i -lt $RemainingArgs.Count; $i++) {
    if ($RemainingArgs[$i] -eq "--port" -and ($i + 1) -lt $RemainingArgs.Count) {
        $port = [int] $RemainingArgs[$i + 1]
    }
}

$existingPids = @(Get-PidsOnPort -Port $port)
if ($existingPids.Count -gt 0) {
    Write-Host "Port $port ist belegt (PID $($existingPids -join ' '))."
    Write-Host "Erst .\kill.ps1 ausfuehren - laeuft evtl. noch ein alter Server."
    exit 1
}

# uv aufloesen: PATH -> astral-uv\ (von install.bat / install.ps1 geladen) ->
# bekannte Installationsorte. Liefert den vollen Pfad zu uv.exe oder $null.
function Find-Uv {
    $cmd = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($cmd) {
        return $cmd.Path
    }

    $dirs = @(
        (Join-Path $PSScriptRoot "astral-uv"),
        (Join-Path $PSScriptRoot "astral-uv\bin")
    )
    if ($env:LOCALAPPDATA) {
        $dirs += (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")
    }
    if ($env:USERPROFILE) {
        $dirs += (Join-Path $env:USERPROFILE ".local\bin")
        $dirs += (Join-Path $env:USERPROFILE ".cargo\bin")
    }

    foreach ($dir in $dirs) {
        $candidate = Join-Path $dir "uv.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

$uvExe = Find-Uv
if (-not $uvExe) {
    Write-Host "uv wurde nicht gefunden (weder im PATH noch unter astral-uv\)."
    Write-Host "Erst install.bat ausfuehren (Doppelklick oder: .\install.bat)."
    exit 1
}
Write-Host "uv: $uvExe"

# Das Verzeichnis des Fundes vorne in den PATH: der versteckte Kindprozess
# unten erbt die Umgebung und ruft "uv" blank auf.
$env:PATH = (Split-Path -Parent $uvExe) + [IO.Path]::PathSeparator + $env:PATH

# Alles lokal halten: managed Python und uv-Cache liegen unter astral-uv\
# neben den Skripten statt im Benutzerprofil (gilt nur fuer diesen Lauf und
# den Kindprozess).
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PSScriptRoot "astral-uv\python"
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot "astral-uv\cache"

& uv sync
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$argsJson = if ($RemainingArgs.Count -gt 0) {
    ConvertTo-Json -InputObject $RemainingArgs -Compress
} else {
    "[]"
}

$argsBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($argsJson))
$powerShellExe = (Get-Process -Id $PID).Path
if (-not $powerShellExe) {
    $powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
}

# Altes server.log vorher wegraeumen: die Warteschleife unten prueft server.log
# auf "Errno|Traceback", der Child-Prozess truncated das Log aber erst nach dem
# Prozessstart - ein Log vom Vorlauf wuerde sonst faelschlich als Startfehler
# gelesen und der Start als gescheitert gemeldet.
Remove-Item -LiteralPath server.log -Force -ErrorAction SilentlyContinue

# Selbstaufruf im Kindprozess-Modus (s. -ServerChild oben). Start-Process
# fuegt die Argumente nur mit Leerzeichen zusammen - der Skriptpfad wird
# deshalb selbst in Anfuehrungszeichen gesetzt (Leerzeichen/Umlaute im Pfad);
# Base64 enthaelt weder Leerzeichen noch Anfuehrungszeichen.
$process = Start-Process -FilePath $powerShellExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $PSCommandPath + '"'),
        "-ServerChild", $argsBase64) `
    -PassThru `
    -WindowStyle Hidden

Set-Content -LiteralPath server.port -Value $port -NoNewline

# Auf die Port-Bindung warten (bis ~30s - langsame PCs brauchen fuer den
# ersten Start deutlich laenger als ein paar Sekunden).
$realPid = $null
for ($i = 0; $i -lt 150; $i++) {
    $realPid = @(Get-PidsOnPort -Port $port | Select-Object -First 1)
    if ($realPid.Count -gt 0) {
        break
    }

    if (Test-Path -LiteralPath server.log) {
        $logText = Get-Content -LiteralPath server.log -Raw -ErrorAction SilentlyContinue
        if ($logText -match "Errno|Traceback") {
            break
        }
    }

    Start-Sleep -Milliseconds 200
}

if ($realPid.Count -gt 0) {
    Set-Content -LiteralPath server.pid -Value $realPid[0] -NoNewline
    $options = if ($RemainingArgs.Count -gt 0) { $RemainingArgs -join " " } else { "keine" }
    Write-Host "Server gestartet (PID $($realPid[0]), Optionen: $options)"
    Write-Host "UI:  http://127.0.0.1:$port"
    Write-Host "Log: server.log | Stoppen: .\kill.ps1"
    try {
        Start-Process "http://127.0.0.1:$port" | Out-Null
    } catch {
        Write-Host "Browser konnte nicht automatisch geoeffnet werden: $($_.Exception.Message)"
    }
} else {
    Write-Host "Server-Start fehlgeschlagen - siehe server.log:"
    if (Test-Path -LiteralPath server.log) {
        Get-Content -LiteralPath server.log -Tail 3
    }
    Remove-Item -LiteralPath server.port -Force -ErrorAction SilentlyContinue
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
