param(
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

function ConvertTo-SingleQuotedLiteral {
    param([string] $Value)
    return "'" + ($Value -replace "'", "''") + "'"
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

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv wurde nicht gefunden. Installation: winget install astral-sh.uv"
    Write-Host "Danach PowerShell neu oeffnen und pruefen mit: uv --version"
    exit 1
}

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
$rootLiteral = ConvertTo-SingleQuotedLiteral -Value $PSScriptRoot

$childScript = @"
Set-Location -LiteralPath $rootLiteral
`$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$argsBase64'))
`$appArgs = @()
if (-not [string]::IsNullOrWhiteSpace(`$json)) {
    `$parsedArgs = ConvertFrom-Json -InputObject `$json
    if (`$null -ne `$parsedArgs) {
        `$appArgs = @(`$parsedArgs)
    }
}
& uv run python -u -m app.server @appArgs *> server.log
"@

$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childScript))
$powerShellExe = (Get-Process -Id $PID).Path
if (-not $powerShellExe) {
    $powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
}

# Altes server.log vorher wegraeumen: die Warteschleife unten prueft server.log
# auf "Errno|Traceback", der Child-Prozess truncated das Log aber erst nach dem
# Prozessstart - ein Log vom Vorlauf wuerde sonst faelschlich als Startfehler
# gelesen und der Start als gescheitert gemeldet.
Remove-Item -LiteralPath server.log -Force -ErrorAction SilentlyContinue

$process = Start-Process -FilePath $powerShellExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encodedCommand) `
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
