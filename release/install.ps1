param(
    [switch] $NoSync,
    [switch] $Force,
    [switch] $Check,
    [switch] $NoPrompt,
    [switch] $Help
)

# Richtet die einzige Voraussetzung des Live-Helpers ein: uv (Astral).
# Alles Weitere (Python, .venv, Abhaengigkeiten) zieht danach "uv sync".
#
# Ablauf:
#   1. Web-Markierung der mitgelieferten *.ps1 entfernen (Unblock-File).
#   2. uv suchen: PATH -> astral-uv\ -> bekannte Installationsorte.
#   3. Nur wenn nichts gefunden wurde: uv nach astral-uv\ neben diesem Skript
#      laden (offizieller Astral-Installer; klappt der nicht, das Release-Zip
#      direkt von GitHub). Kein PATH-Eintrag, kein Admin, keine Profil-Aenderung.
#   4. Optional: Ausfuehrungsrichtlinie auf RemoteSigned (nur nach Rueckfrage).
#   5. "uv sync" - Python und Cache landen lokal unter astral-uv\.
#
# Aufruf am einfachsten per Doppelklick auf install.bat (umgeht die
# Ausfuehrungsrichtlinie nur fuer diesen einen Lauf). Optionen:
#   -NoSync     kein abschliessendes "uv sync"
#   -Force      uv auf jeden Fall neu nach astral-uv\ laden (ersetzt nur die
#               uv-Programme dort; python\ und cache\ bleiben erhalten)
#   -Check      nur melden, ob/wo uv gefunden wird (kein Netz, keine Aenderung);
#               Exit 0 = gefunden, 1 = nicht gefunden
#   -NoPrompt   keine Rueckfrage zur Ausfuehrungsrichtlinie (fuer install.sh)
# Umgebung: UV_VERSION=0.13.5 pinnt die uv-Version (Default: aktuelle).

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1: der Fortschrittsbalken bremst Downloads massiv aus.
$ProgressPreference = "SilentlyContinue"

Set-Location -LiteralPath $PSScriptRoot
$root = $PSScriptRoot
$uvHome = Join-Path $root "astral-uv"

if ($Help) {
    Write-Host "Verwendung: install.bat [-NoSync] [-Force] [-Check] [-NoPrompt]"
    Write-Host "       bzw. .\install.ps1 mit denselben Optionen"
    Write-Host ""
    Write-Host "  -NoSync    kein abschliessendes 'uv sync'"
    Write-Host "  -Force     uv neu nach astral-uv\ laden (python\ und cache\ bleiben)"
    Write-Host "  -Check     nur melden, ob/wo uv gefunden wird (kein Netz)"
    Write-Host "  -NoPrompt  keine Rueckfrage zur Ausfuehrungsrichtlinie"
    Write-Host ""
    Write-Host "Umgebung: UV_VERSION=<version> pinnt die uv-Version."
    exit 0
}

# uv aufloesen: PATH -> astral-uv\ -> bekannte Installationsorte (gleiche
# Reihenfolge wie in start.ps1). Liefert den vollen Pfad zu uv.exe oder $null.
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

# Prozessor-Architektur im Namensschema der uv-Releases (x86_64 / aarch64).
function Get-UvArch {
    $arch = $null
    try {
        $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    } catch {
        $arch = $null
    }
    if (-not $arch) {
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) {
            $arch = $env:PROCESSOR_ARCHITECTURE
        }
    }
    switch -Regex ([string] $arch) {
        '^(x64|amd64)$' { return "x86_64" }
        '^arm64$' { return "aarch64" }
    }
    return $null
}

function New-TempDir {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("uv-install-" + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $dir | Out-Null
    return $dir
}

# Weg 1: offizieller Astral-Installer mit UV_UNMANAGED_INSTALL - legt
# uv.exe/uvx.exe/uvw.exe flach in astral-uv\ ab, ohne PATH- oder Profil-
# Eingriff und ohne Selbst-Update. Laeuft in einem eigenen PowerShell-Prozess,
# damit ein "exit" im fremden Skript nicht dieses Skript beendet.
function Install-ViaAstral {
    if ($env:UV_VERSION) {
        $url = "https://astral.sh/uv/$($env:UV_VERSION)/install.ps1"
    } else {
        $url = "https://astral.sh/uv/install.ps1"
    }
    Write-Host "Methode: offizieller Astral-Installer (Invoke-RestMethod)"
    Write-Host "Quelle:  $url"
    Write-Host "Ziel:    $uvHome"

    $text = Invoke-RestMethod -Uri $url -UseBasicParsing
    if ($text -is [byte[]]) {
        $text = [Text.Encoding]::UTF8.GetString($text)
    }
    $text = ([string] $text).TrimStart([char] 0xFEFF)

    $tmpDir = New-TempDir
    $scriptPath = Join-Path $tmpDir "astral-install.ps1"
    # Mit BOM schreiben, damit Windows PowerShell 5.1 die Datei als UTF-8 liest.
    [IO.File]::WriteAllText($scriptPath, $text, (New-Object System.Text.UTF8Encoding $true))

    $psExe = (Get-Process -Id $PID).Path
    if (-not $psExe) {
        $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    }

    $env:UV_UNMANAGED_INSTALL = $uvHome
    try {
        # Out-Host: die Ausgabe des Kindprozesses soll angezeigt werden und
        # nicht im Rueckgabewert der aufrufenden Funktion landen.
        & $psExe -NoProfile -ExecutionPolicy Bypass -File $scriptPath | Out-Host
        $rc = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($rc -ne 0) {
        throw "Astral-Installer endete mit Code $rc"
    }
}

# Weg 2 (Fallback): das uv-Release-Zip direkt von GitHub laden und entpacken.
function Install-ViaZip {
    $arch = Get-UvArch
    if (-not $arch) {
        throw "Prozessor-Architektur nicht erkannt (unterstuetzt: x86_64, aarch64)."
    }
    $asset = "uv-$arch-pc-windows-msvc.zip"
    if ($env:UV_VERSION) {
        $url = "https://github.com/astral-sh/uv/releases/download/$($env:UV_VERSION)/$asset"
    } else {
        $url = "https://github.com/astral-sh/uv/releases/latest/download/$asset"
    }
    Write-Host "Methode: uv-Release-Zip direkt von GitHub (Invoke-RestMethod)"
    Write-Host "Quelle:  $url"
    Write-Host "Ziel:    $uvHome"

    $tmpDir = New-TempDir
    try {
        $zip = Join-Path $tmpDir $asset
        Invoke-RestMethod -Uri $url -OutFile $zip -UseBasicParsing
        $out = Join-Path $tmpDir "out"
        Expand-Archive -LiteralPath $zip -DestinationPath $out -Force
        $found = @(Get-ChildItem -LiteralPath $out -Recurse -File -Filter "uv*.exe")
        if ($found.Count -eq 0) {
            throw "Im Zip liegt keine uv.exe."
        }
        New-Item -ItemType Directory -Force -Path $uvHome | Out-Null
        foreach ($file in $found) {
            Copy-Item -LiteralPath $file.FullName -Destination $uvHome -Force
        }
    } finally {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-UvLocal {
    New-Item -ItemType Directory -Force -Path $uvHome | Out-Null
    # Nur die uv-Programme ersetzen - python\ und cache\ bleiben erhalten
    # (die .venv zeigt auf das Python darin).
    foreach ($name in @("uv.exe", "uvx.exe", "uvw.exe")) {
        Remove-Item -LiteralPath (Join-Path $uvHome $name) -Force -ErrorAction SilentlyContinue
    }

    $exe = Join-Path $uvHome "uv.exe"
    try {
        Install-ViaAstral
    } catch {
        Write-Host "Astral-Installer fehlgeschlagen: $($_.Exception.Message)"
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Write-Host "Weiche auf das Release-Zip aus ..."
        Install-ViaZip
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "uv.exe fehlt nach der Installation unter $uvHome"
    }
    return $exe
}

# Ausfuehrungsrichtlinie ohne die Prozess-Ebene: install.bat startet dieses
# Skript mit -ExecutionPolicy Bypass, das gilt aber nur fuer diesen einen
# Prozess. Entscheidend ist, ob ein spaeteres ".\start.ps1" in einer neuen
# PowerShell laeuft - also die erste gesetzte Ebene aus CurrentUser und
# LocalMachine (Undefined ueberall = Client-Default Restricted).
function Get-PersistentPolicy {
    foreach ($scope in @("CurrentUser", "LocalMachine")) {
        $policy = [string] (Get-ExecutionPolicy -Scope $scope)
        if ($policy -ne "Undefined") {
            return $policy
        }
    }
    return "Undefined"
}

# Liefert $true, wenn .\start.ps1 danach direkt startbar ist.
function Invoke-PolicyCheck {
    $blocking = @("Restricted", "AllSigned", "Undefined")
    $machinePolicy = [string] (Get-ExecutionPolicy -Scope MachinePolicy)
    $userPolicy = [string] (Get-ExecutionPolicy -Scope UserPolicy)
    if ($machinePolicy -ne "Undefined" -or $userPolicy -ne "Undefined") {
        # Gruppenrichtlinie gewinnt ueber alles - auch ueber -ExecutionPolicy
        # Bypass der .bat-Wrapper und ueber Set-ExecutionPolicy.
        $gpoPolicy = if ($machinePolicy -ne "Undefined") { $machinePolicy } else { $userPolicy }
        if ($blocking -notcontains $gpoPolicy) {
            return $true
        }
        Write-Host "Hinweis: Die Ausfuehrungsrichtlinie ist per Gruppenrichtlinie auf $gpoPolicy"
        Write-Host "festgelegt - das laesst sich weder hier noch per start.bat umgehen."
        Write-Host "Ausweg: ./start.sh unter Git Bash, oder die IT/den Administrator fragen."
        return $false
    }

    $current = Get-PersistentPolicy
    if ($blocking -notcontains $current) {
        return $true
    }
    if ($current -eq "Undefined") {
        $current = "nicht gesetzt = Restricted"
    }

    Write-Host ""
    Write-Host "Die Ausfuehrungsrichtlinie ($current) blockt den direkten Aufruf von .\start.ps1."
    Write-Host "start.bat / kill.bat funktionieren auch ohne Aenderung."
    Write-Host "Optional: Richtlinie nur fuer deinen Benutzer auf RemoteSigned setzen"
    Write-Host "(Set-ExecutionPolicy -Scope CurrentUser RemoteSigned, kein Admin noetig)."
    $answer = ""
    try {
        $answer = Read-Host "Richtlinie jetzt auf RemoteSigned setzen? [j/N]"
    } catch {
        $answer = ""
    }
    if ([string] $answer -notmatch '^\s*(j|ja|y|yes)\s*$') {
        Write-Host "Keine Aenderung an der Ausfuehrungsrichtlinie."
        return $false
    }

    $before = [string] (Get-ExecutionPolicy -Scope CurrentUser)
    try {
        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force -ErrorAction Stop
    } catch {
        # Bekannte Meldung bei Bypass auf Prozess-Ebene ("... overridden by a
        # policy defined at a more specific scope"): die Aenderung ist trotzdem
        # gespeichert - massgeblich ist die Nachkontrolle unten.
        $null = $_
    }
    $after = [string] (Get-ExecutionPolicy -Scope CurrentUser)
    if ($after -eq "RemoteSigned") {
        Write-Host "Geaendert: Ausfuehrungsrichtlinie (Scope CurrentUser) $before -> RemoteSigned."
        Write-Host "Rueckgaengig: Set-ExecutionPolicy -Scope CurrentUser $before"
        return $true
    }
    Write-Host "Aenderung der Ausfuehrungsrichtlinie fehlgeschlagen (CurrentUser=$after)."
    return $false
}

try {
    if ($Check) {
        $found = Find-Uv
        if ($found) {
            Write-Host "uv gefunden: $found"
            exit 0
        }
        Write-Host "uv nicht gefunden (weder im PATH noch unter astral-uv\ oder bekannten Orten)."
        exit 1
    }

    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        $null = $_
    }

    # Web-Markierung (Zone.Identifier) der mitgelieferten Skripte entfernen -
    # sonst blockt RemoteSigned die aus dem Zip entpackten .ps1.
    foreach ($ps1 in @(Get-ChildItem -LiteralPath $root -Filter "*.ps1" -File)) {
        try {
            Unblock-File -LiteralPath $ps1.FullName -ErrorAction Stop
        } catch {
            Write-Host "Unblock-File fehlgeschlagen fuer $($ps1.Name): $($_.Exception.Message)"
        }
    }
    Write-Host "Web-Markierung der *.ps1 im Skriptordner entfernt (Unblock-File)."

    $uvExe = $null
    if ($Force) {
        Write-Host "-Force: uv wird neu nach astral-uv\ geladen."
    } else {
        $uvExe = Find-Uv
    }

    if ($uvExe) {
        Write-Host "uv gefunden: $uvExe - keine Installation noetig."
    } else {
        $uvExe = Install-UvLocal
        Write-Host "uv installiert: $uvExe"
    }

    & $uvExe --version
    if ($LASTEXITCODE -ne 0) {
        throw "uv laesst sich nicht starten ($uvExe)."
    }

    # Wie in start.ps1: gefundenes uv vorne in den PATH, managed Python und
    # uv-Cache lokal unter astral-uv\ (gilt nur fuer diesen Lauf).
    $env:PATH = (Split-Path -Parent $uvExe) + [IO.Path]::PathSeparator + $env:PATH
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $root "astral-uv\python"
    $env:UV_CACHE_DIR = Join-Path $root "astral-uv\cache"

    # Der Richtlinien-Schritt ist optional und darf die Installation nicht
    # abbrechen (z.B. wenn das Security-Modul nicht ladbar ist).
    $directStartOk = $true
    if (-not $NoPrompt) {
        try {
            $directStartOk = Invoke-PolicyCheck
        } catch {
            Write-Host "Hinweis: Ausfuehrungsrichtlinie nicht pruefbar ($($_.Exception.Message))."
            Write-Host "Starten deshalb ueber start.bat / kill.bat."
            $directStartOk = $false
        }
    }

    if ($NoSync) {
        Write-Host "-NoSync: 'uv sync' uebersprungen (laeuft beim ersten Start)."
    } else {
        Write-Host "Richte die Umgebung ein (uv sync: Python + Abhaengigkeiten) ..."
        & $uvExe sync
        if ($LASTEXITCODE -ne 0) {
            $syncCode = $LASTEXITCODE
            Write-Host ""
            Write-Host "Details zum Fehler stehen in der uv-Ausgabe oben."
            Write-Host "Hinweis: Ein Quellcode-Build bzw. 'Visual C++' dort heisst, dass fuer die"
            Write-Host "gewaehlte Python-Version keine fertigen Pakete (Wheels) vorliegen -"
            Write-Host "Python-Pin in .python-version pruefen."
            throw "'uv sync' endete mit Code $syncCode."
        }
    }

    Write-Host ""
    Write-Host "Fertig. Server starten mit:"
    Write-Host "  start.bat     (Doppelklick oder aus cmd/PowerShell)"
    if ($directStartOk) {
        Write-Host "  .\start.ps1   (PowerShell)"
    } else {
        Write-Host "  .\start.ps1   (PowerShell - nur wenn die Ausfuehrungsrichtlinie es zulaesst)"
    }
    Write-Host "  ./start.sh    (Git Bash)"
    Write-Host "Stoppen mit kill.bat / .\kill.ps1 / ./kill.sh"
    exit 0
} catch {
    Write-Host ""
    Write-Host "FEHLER: $($_.Exception.Message)"
    exit 1
}
