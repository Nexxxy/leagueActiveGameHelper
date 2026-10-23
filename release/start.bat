@echo off
rem Startet den Live-Helper-Server ueber start.ps1 (Doppelklick-tauglich).
rem -ExecutionPolicy Bypass gilt nur fuer diesen einen Aufruf - keine bleibende
rem Aenderung an der Ausfuehrungsrichtlinie. Optionen werden durchgereicht,
rem z.B.: start.bat --demo --port 8080
rem PSModulePath leeren (nur lokal): aus einer pwsh-7-Sitzung heraus geerbt,
rem laedt Windows PowerShell 5.1 sonst die pwsh-7-Module und scheitert daran.
setlocal
set "PSModulePath="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo start.ps1 ist mit Fehlercode %RC% beendet worden.
    pause
)
exit /b %RC%
