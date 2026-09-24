"""Manual "close this position now" requests, passed from the dashboard to the bot.

The dashboard is a separate process that only READS the bots' JSON state files, and it
has no exchange credentials — which is correct and worth keeping. So a close button
cannot place an order itself. It writes a request; the bot's own 10-second watcher, the
thing that already owns the position and its stops, picks it up and closes at market.

DESIGN CONSTRAINTS, all learned the hard way elsewhere in this repo:

* A request EXPIRES. A file left behind by a crashed process must not close a brand new
  position hours later — the bot could be flat, re-enter, and then be shut immediately by
  a stale click. Default 10 minutes, which is far longer than the 10s watcher needs.
* Honouring a request CONSUMES it. Anything else closes the position, sees the request
  still sitting there, and tries again on the next tick.
* Unreadable file, unreadable entry, unknown symbol: ignored. A corrupt request file must
  never be able to close a position by accident — the failure direction here is "the
  button did nothing", which the operator sees immediately and can retry.
* Writes are atomic (temp + os.replace). The repo lives on an iCloud-synced Desktop,
  where a half-written file is a thing that actually happens.
"""
import json
import os
import tempfile
import time

DEFAULT_TTL = 10 * 60


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def request_close(path, symbol, now=None):
    """Ask the bot to flatten `symbol`. Returns True if the request is on disk."""
    if not symbol:
        return False
    d = _read(path)
    d[str(symbol)] = float(now if now is not None else time.time())
    return _write(path, d)


def pending_closes(path, now=None, ttl=DEFAULT_TTL):
    """Symbols with a live request. Expired ones are simply not returned."""
    now = float(now if now is not None else time.time())
    out = []
    for sym, ts in _read(path).items():
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            continue
        if 0 <= age <= ttl or (-60 <= age < 0):   # small negative = clock skew, still fresh
            out.append(sym)
    return sorted(out)


def clear_close(path, symbol):
    """Consume the request for `symbol`. Called once the bot has acted on it."""
    d = _read(path)
    if str(symbol) in d:
        d.pop(str(symbol), None)
        return _write(path, d)
    return True


def purge_expired(path, now=None, ttl=DEFAULT_TTL):
    """Drop stale entries so the file cannot grow without bound."""
    now = float(now if now is not None else time.time())
    d = _read(path)
    keep = {}
    for sym, ts in d.items():
        try:
            if (now - float(ts)) <= ttl:
                keep[sym] = ts
        except (TypeError, ValueError):
            continue
    if len(keep) != len(d):
        _write(path, keep)
    return len(d) - len(keep)
