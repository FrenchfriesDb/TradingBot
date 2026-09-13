#!/bin/bash
# Stop the dashboard / trade-journal web server.
# No lock file to consult (see start_chart_server.sh), so match on the command line.
source "$(dirname "$0")/_lib.sh"
cd "$(dirname "$0")/.."

pids=$(pgrep -f chart_server.py)
if [ -z "$pids" ]; then
    echo "chart_server is not running."
    exit 0
fi

echo "Stopping chart_server (PID $(echo "$pids" | tr '\n' ' '))..."
kill $pids 2>/dev/null
waited=0
while [ $waited -lt 10 ]; do
    sleep 1
    waited=$((waited + 1))
    pgrep -f chart_server.py >/dev/null || { echo "✅ Stopped chart_server."; exit 0; }
done
# Verify rather than assume — reporting a stop that didn't happen has burned a whole
# debugging session on this project before.
echo "⚠️  Still alive after 10s — forcing."
kill -9 $(pgrep -f chart_server.py) 2>/dev/null
sleep 1
pgrep -f chart_server.py >/dev/null && echo "❌ Could not stop chart_server." && exit 1
echo "✅ Stopped chart_server."
