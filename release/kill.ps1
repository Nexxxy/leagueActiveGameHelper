# Stoppt den mit start.ps1 gestarteten Live-Helper-Server.
# Robuster als "nur die gemerkte PID killen": Wir beenden das, was den Port
# wirklich haelt - die gemerkte PID kann veraltet sein oder zu einem anderen
# Prozess gehoeren, und ein alter Server ueberlebt sonst den Neustart.

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

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

# Port aus server.port lesen (von start.ps1 abgelegt), sonst Default 8000.
$port = 8000
if (Test-Path -LiteralPath server.port) {
    $portText = (Get-Content -LiteralPath server.port -Raw -ErrorAction SilentlyContinue)
    if ($portText) {
        $portText = $portText.Trim()
        if ($portText -match '^\d+$') {
            $port = [int] $portText
        }
    }
}

# Zu beendende Prozesse: was den Port haelt, plus die gemerkte PID.
$targets = New-Object System.Collections.Generic.List[int]
foreach ($portPid in @(Get-PidsOnPort -Port $port)) {
    $targets.Add([int] $portPid)
}

if (Test-Path -LiteralPath server.pid) {
    $pidText = (Get-Content -LiteralPath server.pid -Raw -ErrorAction SilentlyContinue)
    if ($pidText) {
        $pidText = $pidText.Trim()
        if ($pidText -match '^\d+$') {
            $targets.Add([int] $pidText)
        }
    }
}

$killed = $false
foreach ($targetPid in @($targets | Sort-Object -Unique)) {
    try {
        Stop-Process -Id $targetPid -Force -ErrorAction Stop
        Write-Host "Server (PID $targetPid) beendet."
        $killed = $true
    } catch {
        # Prozess laeuft nicht mehr oder darf nicht beendet werden - weiter.
    }
}

if (-not $killed) {
    Write-Host "Kein laufender Server auf Port $port gefunden."
}

Remove-Item -LiteralPath server.pid -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath server.port -Force -ErrorAction SilentlyContinue
