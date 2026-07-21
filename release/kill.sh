#!/usr/bin/env bash
# Stoppt den mit start.sh gestarteten Live-Helper-Server.
# Robuster als "nur die gemerkte PID killen": Wir beenden das, was den Port
# wirklich haelt - unter Git Bash weicht die gemerkte Bash-PID sonst von der
# echten python.exe-PID ab, und ein alter Server ueberlebt den Neustart.
cd "$(dirname "$0")"

PORT=8000
[ -f server.port ] && PORT=$(cat server.port)

# PIDs, die auf dem Port LAUSCHEN. Sprachunabhaengig: der Status ("LISTENING"
# / "ABHOEREN") ist lokalisiert, die Lausch-Zeile aber immer daran erkennbar,
# dass die Remote-Adresse 0.0.0.0:0 bzw. [::]:0 ist. Spalten: 2=lokal, 5=PID.
pids_on_port() {
    netstat -ano 2>/dev/null | awk -v p=":$1\$" \
        '$2 ~ p && ($3 == "0.0.0.0:0" || $3 == "[::]:0") { print $5 }' \
        | grep -E '^[0-9]+$' | sort -u
}

killed=""
for PID in $(pids_on_port "$PORT") $( [ -f server.pid ] && cat server.pid ); do
    [ -z "$PID" ] && continue
    if taskkill //F //PID "$PID" >/dev/null 2>&1 || kill "$PID" 2>/dev/null; then
        echo "Server (PID $PID) beendet."
        killed="yes"
    fi
done

[ -z "$killed" ] && echo "Kein laufender Server auf Port $PORT gefunden."
rm -f server.pid server.port
