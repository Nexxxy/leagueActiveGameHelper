#!/usr/bin/env bash
# Richtet die einzige Voraussetzung des Live-Helpers ein: uv (Astral).
# Alles Weitere (Python, .venv, Abhaengigkeiten) zieht danach "uv sync".
# Nur Windows: laeuft unter Git Bash / MSYS2 (kein Linux/macOS).
#
# Ablauf: uv suchen (PATH -> astral-uv/ -> bekannte Orte). Nur wenn nichts
# gefunden wird, uv nach astral-uv/ neben diesem Skript laden - mit dem ersten
# vorhandenen Werkzeug aus curl, wget, PowerShell (powershell.exe, sonst pwsh;
# dann uebernimmt install.ps1 den kompletten Lauf). Keine Rueckfragen, kein
# PATH-Eintrag, kein Admin. Zum Schluss "uv sync" - managed Python und
# uv-Cache liegen dabei lokal unter astral-uv/.
#
# Optionen:
#   --check     nur melden, ob/wo uv gefunden wird (kein Netz, keine
#               Aenderung); Exit 0 = gefunden, 1 = nicht gefunden
#   --force     uv auf jeden Fall neu nach astral-uv/ laden (ersetzt nur die
#               uv-Programme dort; python/ und cache/ bleiben erhalten)
#   --no-sync   kein abschliessendes "uv sync"
#   -h, --help  diese Hilfe
# Umgebung: UV_VERSION=0.13.5 pinnt die uv-Version (Default: aktuelle).
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
UV_HOME="$ROOT/astral-uv"

usage() {
    cat <<'USAGE'
Verwendung: bash install.sh [--check] [--force] [--no-sync] [-h|--help]

  --check     nur melden, ob/wo uv gefunden wird (kein Netz); Exit 0/1
  --force     uv neu nach astral-uv/ laden (python/ und cache/ bleiben)
  --no-sync   kein abschliessendes 'uv sync'
  -h, --help  diese Hilfe

Umgebung: UV_VERSION=<version> pinnt die uv-Version.
USAGE
}

CHECK=0
FORCE=0
SYNC=1
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --force) FORCE=1 ;;
        --no-sync) SYNC=0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "FEHLER: Unbekanntes Argument '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

to_unix() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else echo "$1"; fi
}
to_native() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi
}

# uv aufloesen: PATH -> astral-uv/ -> bekannte Installationsorte (gleiche
# Reihenfolge wie in start.sh). Gibt den Pfad aus, Rueckgabe 1 = nichts da.
find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    local d
    for d in "$ROOT/astral-uv" "$ROOT/astral-uv/bin" \
             "${LOCALAPPDATA:+$(to_unix "$LOCALAPPDATA")/Microsoft/WinGet/Links}" \
             "${USERPROFILE:+$(to_unix "$USERPROFILE")/.local/bin}" \
             "${HOME:+$HOME/.local/bin}" \
             "${USERPROFILE:+$(to_unix "$USERPROFILE")/.cargo/bin}" \
             "${HOME:+$HOME/.cargo/bin}"; do
        [ -n "$d" ] || continue
        if [ -f "$d/uv.exe" ]; then echo "$d/uv.exe"; return 0; fi
        if [ -f "$d/uv" ]; then echo "$d/uv"; return 0; fi
    done
    return 1
}

if [ "$CHECK" = 1 ]; then
    if UV_BIN="$(find_uv)"; then
        echo "uv gefunden: $UV_BIN"
        exit 0
    fi
    echo "uv nicht gefunden (weder im PATH noch unter astral-uv/ oder bekannten Orten)."
    exit 1
fi

# Fuer einen PowerShell-Befehl in einfachen Anfuehrungszeichen quoten.
ps_quote() {
    local s="$1"
    printf "'%s'" "${s//\'/\'\'}"
}

find_powershell() {
    local c
    for c in powershell.exe pwsh.exe pwsh; do
        if command -v "$c" >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

# Prozessor-Architektur im Namensschema der uv-Releases (x86_64 / aarch64).
uv_arch() {
    local a="${PROCESSOR_ARCHITEW6432:-${PROCESSOR_ARCHITECTURE:-}}"
    case "$a" in
        AMD64|amd64|x86_64) echo x86_64; return 0 ;;
        ARM64|arm64|aarch64) echo aarch64; return 0 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) echo x86_64 ;;
        aarch64|arm64) echo aarch64 ;;
        *) return 1 ;;
    esac
}

fetch() {
    case "$TOOL" in
        curl) curl -fsSL --retry 2 -o "$2" "$1" ;;
        wget) wget -q -O "$2" "$1" ;;
        *) return 1 ;;
    esac
}

# Zip entpacken: unzip, sonst das Windows-eigene tar.exe (bsdtar kann zip -
# das GNU-tar von Git Bash nicht), sonst PowerShell Expand-Archive.
extract_zip() {
    local zip="$1" out="$2" systar ps
    if command -v unzip >/dev/null 2>&1 && unzip -q -o "$zip" -d "$out"; then
        return 0
    fi
    systar="$(to_unix "${SYSTEMROOT:-C:\\Windows}")/System32/tar.exe"
    if [ -f "$systar" ] && (cd "$out" && "$systar" -xf "$(to_native "$zip")"); then
        return 0
    fi
    if ps="$(find_powershell)"; then
        "$ps" -NoProfile -NonInteractive -Command \
            "Expand-Archive -LiteralPath $(ps_quote "$(to_native "$zip")") -DestinationPath $(ps_quote "$(to_native "$out")") -Force" \
            && return 0
    fi
    return 1
}

# Weg 1: offizieller Astral-Installer mit UV_UNMANAGED_INSTALL - legt
# uv.exe/uvx.exe/uvw.exe flach in astral-uv/ ab, ohne PATH- oder Profil-
# Eingriff und ohne Selbst-Update.
install_via_astral() {
    local url
    if [ -n "${UV_VERSION:-}" ]; then
        url="https://astral.sh/uv/$UV_VERSION/install.sh"
    else
        url="https://astral.sh/uv/install.sh"
    fi
    echo "Methode: offizieller Astral-Installer ($TOOL)"
    echo "Quelle:  $url"
    echo "Ziel:    $UV_HOME"
    fetch "$url" "$TMP_UV/astral-install.sh" || return 1
    UV_UNMANAGED_INSTALL="$UV_HOME" sh "$TMP_UV/astral-install.sh" || return 1
}

# Weg 2 (Fallback): das uv-Release-Zip direkt von GitHub laden und entpacken.
install_via_zip() {
    local arch asset url f n=0
    if ! arch="$(uv_arch)"; then
        echo "Prozessor-Architektur nicht erkannt (unterstuetzt: x86_64, aarch64)." >&2
        return 1
    fi
    asset="uv-$arch-pc-windows-msvc.zip"
    if [ -n "${UV_VERSION:-}" ]; then
        url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$asset"
    else
        url="https://github.com/astral-sh/uv/releases/latest/download/$asset"
    fi
    echo "Methode: uv-Release-Zip direkt von GitHub ($TOOL)"
    echo "Quelle:  $url"
    echo "Ziel:    $UV_HOME"
    fetch "$url" "$TMP_UV/$asset" || return 1
    mkdir -p "$TMP_UV/out" "$UV_HOME" || return 1
    if ! extract_zip "$TMP_UV/$asset" "$TMP_UV/out"; then
        echo "Entpacken fehlgeschlagen (weder unzip, tar.exe noch PowerShell)." >&2
        return 1
    fi
    while IFS= read -r f; do
        cp -f "$f" "$UV_HOME/" && n=$((n + 1))
    done < <(find "$TMP_UV/out" -type f -name 'uv*.exe')
    [ "$n" -gt 0 ]
}

UV_BIN=""
if [ "$FORCE" = 1 ]; then
    echo "--force: uv wird neu nach astral-uv/ geladen."
elif UV_BIN="$(find_uv)"; then
    echo "uv gefunden: $UV_BIN - keine Installation noetig."
fi

if [ -z "$UV_BIN" ]; then
    # Erstes vorhandenes Werkzeug: curl -> wget -> PowerShell.
    TOOL=""
    PS_EXE=""
    if command -v curl >/dev/null 2>&1; then
        TOOL=curl
    elif command -v wget >/dev/null 2>&1; then
        TOOL=wget
    elif PS_EXE="$(find_powershell)"; then
        TOOL=powershell
    else
        echo "FEHLER: Kein Download-Werkzeug gefunden (curl, wget oder PowerShell)." >&2
        exit 1
    fi

    if [ "$TOOL" = powershell ]; then
        # install.ps1 uebernimmt den kompletten Lauf (Download, Pruefung, Sync).
        # -Force: hier wurde schon gesucht und nichts gefunden - also laden.
        # -NoPrompt: install.sh stellt keine Rueckfragen.
        if [ ! -f "$ROOT/install.ps1" ]; then
            echo "FEHLER: install.ps1 fehlt neben install.sh." >&2
            exit 1
        fi
        echo "Methode: PowerShell ($PS_EXE) -> install.ps1"
        PS_ARGS=(-NoProfile -ExecutionPolicy Bypass -File "$(to_native "$ROOT/install.ps1")" -Force -NoPrompt)
        [ "$SYNC" = 0 ] && PS_ARGS+=(-NoSync)
        "$PS_EXE" "${PS_ARGS[@]}"
        exit $?
    fi

    TMP_UV="$(mktemp -d)" || exit 1
    trap 'rm -rf "$TMP_UV"' EXIT
    mkdir -p "$UV_HOME" || exit 1
    # Nur die uv-Programme ersetzen - python/ und cache/ bleiben erhalten
    # (die .venv zeigt auf das Python darin).
    rm -f "$UV_HOME/uv.exe" "$UV_HOME/uvx.exe" "$UV_HOME/uvw.exe"

    if ! install_via_astral; then
        echo "Astral-Installer fehlgeschlagen."
    fi
    if [ ! -f "$UV_HOME/uv.exe" ]; then
        echo "Weiche auf das Release-Zip aus ..."
        install_via_zip || true
    fi
    if [ ! -f "$UV_HOME/uv.exe" ]; then
        echo "FEHLER: uv.exe fehlt nach der Installation unter $UV_HOME" >&2
        exit 1
    fi
    UV_BIN="$UV_HOME/uv.exe"
    echo "uv installiert: $UV_BIN"
fi

if ! "$UV_BIN" --version; then
    echo "FEHLER: uv laesst sich nicht starten ($UV_BIN)." >&2
    exit 1
fi

# Wie in start.sh: gefundenes uv vorne in den PATH, managed Python und
# uv-Cache lokal unter astral-uv/ (gilt nur fuer diesen Lauf).
export PATH="$(dirname "$UV_BIN"):$PATH"
export UV_PYTHON_INSTALL_DIR="$(to_native "$ROOT/astral-uv/python")"
export UV_CACHE_DIR="$(to_native "$ROOT/astral-uv/cache")"

if [ "$SYNC" = 1 ]; then
    echo "Richte die Umgebung ein (uv sync: Python + Abhaengigkeiten) ..."
    if ! "$UV_BIN" sync; then
        echo "FEHLER: 'uv sync' ist fehlgeschlagen - Details in der uv-Ausgabe oben." >&2
        echo "Hinweis: Ein Quellcode-Build bzw. 'Visual C++' dort heisst, dass fuer die" >&2
        echo "gewaehlte Python-Version keine fertigen Pakete (Wheels) vorliegen -" >&2
        echo "Python-Pin in .python-version pruefen." >&2
        exit 1
    fi
else
    echo "--no-sync: 'uv sync' uebersprungen (laeuft beim ersten Start)."
fi

echo ""
echo "Fertig. Server starten mit:"
echo "  ./start.sh    (Git Bash; aus dem Zip entpackt ggf. bash start.sh)"
echo "  start.bat     (Doppelklick) bzw. .\\start.ps1 (PowerShell)"
echo "Stoppen mit ./kill.sh bzw. kill.bat / .\\kill.ps1"
