#!/bin/bash
# Shared start/stop logic for TradingBot processes. Sourced by the per-bot scripts in
# this directory — not meant to be run directly.
#
# Always launches detached (nohup + disown) so the bot survives the terminal closing,
# and with `caffeinate -i -s` so it resists BOTH idle sleep and lid-close system sleep
# — every prior manual launch in this project only used `-i`, which doesn't cover
# lid-close, and ran in the foreground with no nohup/disown, so the bot died the
# instant the terminal session ended for any reason.
#
# Uses the SAME lock file the bot's own Python code writes (bot/single_instance_lock.py)
# to find its real PID — no pkill pattern-matching, which has previously matched
# nothing because these processes run as ".../Python binance_bot.py", not
# "python3 binance_bot.py".

# ── launchd awareness ─────────────────────────────────────────────────────────
# launchd may own these bots via ~/Library/LaunchAgents/com.debbiela.<name>.plist with
# KeepAlive=true. When it does, a plain `kill` is theatre: launchd revives the bot within
# its ThrottleInterval while this script still prints "Stopped". That exact confusion
# cost a real debugging session — stop_stock_bot.sh reported success three times in a row
# against a bot that never actually stopped. So every entry point below asks launchd
# first and treats it as the owner when it is one.
_LD_PREFIX="com.debbiela"
_ld_label()  { printf '%s.%s' "$_LD_PREFIX" "$1"; }
_ld_domain() { printf 'gui/%s' "$(id -u)"; }
_ld_plist()  { printf '%s/Library/LaunchAgents/%s.plist' "$HOME" "$(_ld_label "$1")"; }

# 0 = launchd has this job loaded right now (it will restart the bot if we kill it)
_ld_manages()   { launchctl print "$(_ld_domain)/$(_ld_label "$1")" >/dev/null 2>&1; }
# 0 = a plist exists on disk, so launchd will load it again at the next login even if
#     it is unloaded at this moment. Worth saying out loud when we unload one.
_ld_installed() { [ -f "$(_ld_plist "$1")" ]; }

# launchd cannot write into the iCloud-synced Desktop, so its jobs log to ~/Library/Logs.
# A watcher tailing logs/<name>.log under launchd sees a file frozen days ago and
# concludes the bot is dead. Resolve to whichever log is actually being written.
_ld_logfile()   { printf '%s/Library/Logs/debbiela/%s.log' "$HOME" "$1"; }
_bot_logfile() {
    local name="$1"
    if _ld_manages "$name" && [ -f "$(_ld_logfile "$name")" ]; then
        _ld_logfile "$name"
    else
        printf 'logs/%s.log' "$name"
    fi
}


_bot_start() {
    local name="$1" lock_file="$2"; shift 2
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
    mkdir -p logs

    # If a plist is installed, launchd is the owner — start it THERE, not with our own
    # nohup. Starting a second, unsupervised copy here is how you end up with the lock
    # bouncing between two managers and no auto-restart on the one that survives.
    if _ld_installed "$name"; then
        if _ld_manages "$name"; then
            local ld_pid
            ld_pid=$(launchctl list | awk -v l="$(_ld_label "$name")" '$3==l {print $1}')
            if [ -n "$ld_pid" ] && [ "$ld_pid" != "-" ]; then
                echo "✅ $name is already running under launchd (PID $ld_pid)."
            else
                launchctl kickstart "$(_ld_domain)/$(_ld_label "$name")" >/dev/null 2>&1
                echo "✅ Started $name via launchd."
            fi
        else
            launchctl bootstrap "$(_ld_domain)" "$(_ld_plist "$name")" 2>/dev/null
            echo "✅ Started $name via launchd (supervision re-enabled)."
        fi
        echo "   logs: $(_ld_logfile "$name")"
        echo "   watch: scripts/watch_${name}.sh"
        return 0
    fi

    if [ -f "$lock_file" ] && kill -0 "$(cat "$lock_file" 2>/dev/null)" 2>/dev/null; then
        echo "⚠️  $name is already running (PID $(cat "$lock_file")) — not starting a duplicate."
        echo "    Run scripts/stop_${name}.sh first if you really want to restart."
        exit 1
    fi
    local log_file="logs/${name}.log"
    # PYTHONUNBUFFERED: when stdout isn't a terminal (redirected to a file, as here),
    # Python fully buffers it instead of line-buffering — output sits in memory and
    # never reaches the log file until the internal buffer fills or the process exits.
    # Without this, `tail -f` on the log looks completely dead even while the bot is
    # actively running and printing.
    PYTHONUNBUFFERED=1 nohup caffeinate -i -s "$@" > "$log_file" 2>&1 &
    # Capture the PID BEFORE disown — `$!` was previously read after it, which made the
    # liveness check below report a false "exited immediately" on a bot that had in fact
    # started fine. That false alarm is what pushed the operator into relaunching bare in
    # a terminal (the recurring frozen-log problem), so this check has to be trustworthy.
    local wrapper_pid=$!
    disown
    # The real proof of life is the bot's OWN lock file: its Python writes it early in
    # startup with its real PID. Poll for it rather than trusting the wrapper's liveness,
    # since `caffeinate` outlives a Python process that dies on import.
    local waited=0
    while [ $waited -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
        local bot_pid
        bot_pid=$(cat "$lock_file" 2>/dev/null)
        if [ -n "$bot_pid" ] && kill -0 "$bot_pid" 2>/dev/null; then
            echo "✅ Started $name (PID $bot_pid) — logs: $log_file"
            return 0
        fi
        if ! kill -0 "$wrapper_pid" 2>/dev/null; then
            echo "⚠️  $name exited immediately — check $log_file"
            return 1
        fi
    done
    # Still no lock after 10s but the wrapper lives: slow start (macOS Gatekeeper rescans
    # every .so on the first import after a reboot, which can take many minutes). Not an
    # error — say so plainly instead of implying a crash.
    echo "✅ Started $name (PID $wrapper_pid, still initializing) — logs: $log_file"
}

# Run a bot IN THIS WINDOW (foreground), the way `caffeinate -i python3 X.py` did — but
# piped through `tee` so logs/X.log is still written. That log is what makes a trade
# explainable after the fact; without it the reasoning has to be reconstructed from raw
# candle replay. Ctrl-C or closing the window DOES stop the bot here (that's the tradeoff
# vs. _bot_start); use scripts/start_X.sh + scripts/watch_X.sh if you want it to survive.
_bot_run_foreground() {
    local name="$1" lock_file="$2"; shift 2
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
    mkdir -p logs

    # A launchd-managed copy holds the lock, so running here would just hit
    # "another instance is already running" — and killing its PID would only make
    # launchd restart it. Unload the job first.
    if _ld_manages "$name"; then
        echo "🔒 Unloading the launchd job so $name can run in this window instead…"
        launchctl bootout "$(_ld_domain)/$(_ld_label "$name")" 2>/dev/null
        sleep 2
        rm -f "$lock_file"
        echo "   ⚠️  Supervision is OFF until you run scripts/start_${name}.sh again."
    fi

    # A detached instance would hold the lock and refuse this one, so clear it first —
    # the whole point of this script is "I want it here, in front of me".
    local existing
    existing=$(cat "$lock_file" 2>/dev/null)
    if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
        echo "ℹ️  Stopping the detached $name (PID $existing) so it can run here instead…"
        local eppid
        eppid=$(ps -o ppid= -p "$existing" 2>/dev/null | tr -d ' ')
        if [ -n "$eppid" ] && [ "$eppid" != "1" ]; then
            kill "$existing" "$eppid" 2>/dev/null
        else
            kill "$existing" 2>/dev/null
        fi
        sleep 2
        rm -f "$lock_file"
    fi
    echo "▶️  Running $name in this window. Ctrl-C or closing it STOPS THE BOT."
    echo "   (logging to logs/${name}.log as it goes)"
    echo "────────────────────────────────────────────────────────────"
    # -a appends: this script gets restarted by hand a lot, and truncating on every run
    # would throw away the history that makes past trades explainable.
    # PYTHONUNBUFFERED: `tee` is a pipe, not a terminal, so Python would otherwise block-
    # buffer and the window would sit blank for long stretches.
    PYTHONUNBUFFERED=1 caffeinate -i -s "$@" 2>&1 | tee -a "logs/${name}.log"
}

# Watch a running bot's output live in this terminal, exactly like it looked when it ran
# in the foreground — but the bot itself stays detached, so closing this window (or Ctrl-C
# here) only stops WATCHING, never the bot. Wanting to see the output on screen is the
# reason bots kept getting relaunched bare in a terminal, which froze logs/*.log for days
# at a time; this gives the same live view without that cost.
_bot_watch() {
    local name="$1" lock_file="$2"
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
    # Under launchd the live log is ~/Library/Logs/debbiela/, NOT logs/ — tailing the
    # stale logs/ copy makes a perfectly healthy bot look dead for days.
    local log_file
    log_file="$(_bot_logfile "$name")"
    local pid
    pid=$(cat "$lock_file" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        if _ld_manages "$name"; then
            echo "👀 Watching $name (PID $pid, supervised by launchd) — Ctrl-C stops watching, NOT the bot."
        else
            echo "👀 Watching $name (PID $pid) — Ctrl-C stops watching, NOT the bot."
        fi
        echo "   log: $log_file"
    else
        echo "⚠️  $name does not appear to be running (no live PID in $lock_file)."
        echo "    Showing $log_file anyway; start it with scripts/start_${name}.sh"
    fi
    echo "────────────────────────────────────────────────────────────"
    # -n 40: a bit of scrollback so the window isn't blank until the next 5-min tick.
    tail -n 40 -F "$log_file"
}

_bot_stop() {
    local name="$1" lock_file="$2"
    cd "$(dirname "${BASH_SOURCE[0]}")/.."

    # launchd owns it -> bootout is the ONLY real stop. Killing the PID just hands
    # launchd a restart trigger (KeepAlive), which is why this script used to report
    # "✅ Stopped" while the bot came straight back with a new PID.
    if _ld_manages "$name"; then
        echo "🔒 $name is supervised by launchd — unloading the job (a plain kill would"
        echo "   just be restarted within ~30s)."
        launchctl bootout "$(_ld_domain)/$(_ld_label "$name")" 2>/dev/null

        # Verify rather than assume: bootout is asynchronous.
        local waited=0 pid
        while [ $waited -lt 10 ]; do
            pid=$(cat "$lock_file" 2>/dev/null)
            if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then break; fi
            sleep 1; waited=$((waited + 1))
        done
        pid=$(cat "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  launchd job unloaded but PID $pid is still alive — killing it directly."
            kill "$pid" 2>/dev/null; sleep 2
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$lock_file"
        echo "✅ Stopped $name, and it will NOT come back on its own."
        if _ld_installed "$name"; then
            echo "   ⚠️  Supervision is now OFF (it would otherwise reload at next login)."
            echo "   Re-enable with: scripts/start_${name}.sh"
        fi
        return 0
    fi

    if [ ! -f "$lock_file" ]; then
        echo "No lock file found for $name — is it running? (checked: $lock_file)"
        exit 1
    fi
    local pid
    pid=$(cat "$lock_file")
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "Lock file PID $pid is not running (stale lock) — removing lock file."
        rm -f "$lock_file"
        exit 0
    fi
    local ppid
    ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$ppid" ] && [ "$ppid" != "1" ]; then
        echo "Stopping $name (PID $pid) and its caffeinate wrapper (PID $ppid)..."
        kill "$pid" "$ppid" 2>/dev/null
    else
        echo "Stopping $name (PID $pid)..."
        kill "$pid" 2>/dev/null
    fi
    sleep 1
    rm -f "$lock_file"
    echo "✅ Stopped $name."
}
