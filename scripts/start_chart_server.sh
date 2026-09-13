#!/bin/bash
# Start the dashboard / trade-journal web server (chart_server.py), detached.
#
# Unlike the three bots, chart_server.py writes no lock file (bot/single_instance_lock.py
# is a trading-safety guard — two dashboards are harmless, two bots are not), so this
# finds it by command line instead. `pgrep -f chart_server.py` is the pattern that
# actually matches: these run as ".../Python chart_server.py", NOT "python3 chart_server.py",
# which is why plain `pkill python3` has historically matched nothing here.
#
# Detached on purpose. A server started from a shell that later goes away can end up
# holding a stale, restricted execution context: on 2026-08-31 one such instance kept
# serving /api/botstatus while every open() of templates/journal.html returned EPERM,
# so /journal 500'd for a day with the file perfectly intact. Restarting is the fix,
# which is the whole reason this script exists.
source "$(dirname "$0")/_lib.sh"
cd "$(dirname "$0")/.."
mkdir -p logs

existing=$(pgrep -f chart_server.py | head -1)
if [ -n "$existing" ]; then
    echo "✅ chart_server is already running (PID $existing) — http://localhost:8888"
    echo "   Run scripts/stop_chart_server.sh first if you want a clean restart."
    exit 0
fi

log_file="logs/chart_server.log"
# PYTHONUNBUFFERED: stdout is a file here, not a terminal, so Python block-buffers and
# the log looks frozen while the server is perfectly healthy.
PYTHONUNBUFFERED=1 nohup python3 chart_server.py > "$log_file" 2>&1 &
wrapper_pid=$!
disown

# Proof of life is a real HTTP 200 on the route that reads the template from disk —
# not just "the process exists". A process that is up but can't read templates/ is
# exactly the failure this script was written for, and it must not report success.
waited=0
while [ $waited -lt 15 ]; do
    sleep 1
    waited=$((waited + 1))
    if ! kill -0 "$wrapper_pid" 2>/dev/null; then
        echo "⚠️  chart_server exited immediately — check $log_file"
        exit 1
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:8888/journal?account=crypto" 2>/dev/null)
    if [ "$code" = "200" ]; then
        echo "✅ Started chart_server (PID $(pgrep -f chart_server.py | head -1)) — logs: $log_file"
        echo "   Dashboard → http://localhost:8888"
        echo "   Journal   → http://localhost:8888/journal"
        exit 0
    fi
done
echo "⚠️  chart_server is up (PID $wrapper_pid) but /journal did not return 200 within 15s."
echo "    Check $log_file — a PermissionError there means the process needs a restart."
exit 1
