@echo off
rem Startet install.ps1 (laedt uv nach astral-uv\ und richtet die Umgebung ein).
rem -ExecutionPolicy Bypass gilt nur fuer diesen einen Aufruf - keine bleibende
rem Aenderung an der Ausfuehrungsrichtlinie. Optionen werden durchgereicht,
rem z.B.: install.bat -NoSync
rem Das Fenster wartet am Ende immer auf eine Taste; der Exitcode von
rem install.ps1 bleibt erhalten.
rem PSModulePath leeren (nur lokal): aus einer pwsh-7-Sitzung heraus geerbt,
rem laedt Windows PowerShell 5.1 sonst die pwsh-7-Module und scheitert daran.
setlocal
set "PSModulePath="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo install.ps1 ist mit Fehlercode %RC% beendet worden.
)
rem pause immer (auch bei Erfolg): beim Doppelklick bliebe das Fenster sonst
rem nicht offen und die Abschlussmeldung waere nicht lesbar.
echo.
pause
exit /b %RC%
