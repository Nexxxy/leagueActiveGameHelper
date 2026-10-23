#!/usr/bin/env bash
# Startet den Live-Helper-Server im Hintergrund.
# Alle Optionen werden durchgereicht, z.B.:
#   ./start.sh --demo
#   ./start.sh --demo --port 8080
cd "$(dirname "$0")"

# Port aus den Argumenten ziehen (Default 8000) - vor dem Start, damit wir
# zuverlaessig gegen "Port schon belegt" pruefen koennen.
PORT=8000
prev=""
for arg in "$@"; do
    [ "$prev" = "--port" ] && PORT="$arg"
    prev="$arg"
done

# PIDs, die den Port halten. Sprachunabhaengig: der netstat-Status ist
# lokalisiert ("LISTENING"/"ABHOEREN"), die Lausch-Zeile aber immer daran
# erkennbar, dass die Remote-Adresse 0.0.0.0:0 bzw. [::]:0 ist.
pids_on_port() {
    netstat -ano 2>/dev/null | awk -v p=":$1\$" \
        '$2 ~ p && ($3 == "0.0.0.0:0" || $3 == "[::]:0") { print $5 }' \
        | grep -E '^[0-9]+$' | sort -u
}

# Der eigentliche "laeuft schon?"-Check: haelt jemand den Port? Das ist
# zuverlaessiger als eine gemerkte PID (die unter Git Bash abweichen kann).
if [ -n "$(pids_on_port "$PORT")" ]; then
    echo "Port $PORT ist belegt (PID $(pids_on_port "$PORT" | tr '\n' ' '))."
    echo "Erst ./kill.sh ausfuehren - laeuft evtl. noch ein alter Server."
    exit 1
fi

# uv aufloesen: PATH -> astral-uv/ (von install.sh / install.bat geladen) ->
# bekannte Installationsorte. Das Verzeichnis des Fundes kommt vorne in den
# PATH, damit auch der Hintergrundprozess unten genau dieses uv nimmt.
ROOT="$(pwd)"
to_unix() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else echo "$1"; fi
}
to_native() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi
}
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
if ! UV_BIN="$(find_uv)"; then
    echo "uv wurde nicht gefunden (weder im PATH noch unter astral-uv/)."
    echo "Erst ./install.sh (bzw. install.bat unter Windows) ausfuehren."
    exit 1
fi
echo "uv: $UV_BIN"
export PATH="$(dirname "$UV_BIN"):$PATH"

# Alles lokal halten: managed Python und uv-Cache liegen unter astral-uv/
# neben den Skripten statt im Benutzerprofil (gilt nur fuer diesen Lauf).
export UV_PYTHON_INSTALL_DIR="$(to_native "$ROOT/astral-uv/python")"
export UV_CACHE_DIR="$(to_native "$ROOT/astral-uv/cache")"

# venv synchron halten (uv.lock) - bewusst VOR dem Hintergrundstart, damit
# eine laengere Erstinstallation nicht den Port-Warte-Timeout unten reisst.
uv sync || exit 1
uv run python -u -m app.server "$@" > server.log 2>&1 &
BASH_PID=$!
echo "$PORT" > server.port

# Auf die Port-Bindung warten (bis ~30s - langsame PCs brauchen fuer den
# ersten Start deutlich laenger als ein paar Sekunden). Die Bash-Job-PID ($!)
# ist unter Git Bash oft nicht die des Python-Prozesses - die echte PID holen
# wir ueber den Port, sobald er lauscht.
REAL_PID=""
for _ in $(seq 1 150); do
    REAL_PID="$(pids_on_port "$PORT" | head -n1)"
    [ -n "$REAL_PID" ] && break
    grep -qE "Errno|Traceback" server.log 2>/dev/null && break
    sleep 0.2
done

if [ -n "$REAL_PID" ]; then
    echo "$REAL_PID" > server.pid
    echo "Server gestartet (PID $REAL_PID, Optionen: ${*:-keine})"
    echo "UI:  http://127.0.0.1:$PORT"
    echo "Log: server.log | Stoppen: ./kill.sh"
    explorer.exe "http://127.0.0.1:$PORT" >/dev/null 2>&1 &
else
    echo "Server-Start fehlgeschlagen - siehe server.log:"
    tail -n 3 server.log
    rm -f server.port
    exit 1
fi
