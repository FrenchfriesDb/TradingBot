"""A durable local ledger, written BEFORE the Sheets call.

Eight closed trades never reached the 'Crypto Ledger' tab:

    [SHEETS] log_trade to 'Crypto Ledger' failed: ('Connection aborted.',
             ConnectionResetError(54, 'Connection reset by peer'))                x2
    [SHEETS] log_trade to 'Crypto Ledger' failed: OSError(65, 'No route to host') x2
    [SHEETS] Auth failed (google_credentials.json): [Errno 11] Resource deadlock avoided

log_trade() is fail-soft by design — it logs a warning and returns rather than crashing
the trading loop, which is right. But it was also fire-and-forget: no retry, no queue.
A transient network blip, or the iCloud EDEADLK that took every bot down on 2026-09-23,
and that trade is gone. Not delayed — gone. The journal then shows a P&L curve with
holes in it and no indication that anything is missing, which is worse than an outage
because it still looks complete.

So the row is appended HERE first, to local disk, and only then handed to Sheets. A
failed send leaves the row pending and a later flush retries it. Nothing is ever dropped
because a network call chose a bad moment.

Deliberately:

* JSON Lines, append-only. One self-contained row per line, so a process killed
  mid-write corrupts at most the last line and every earlier trade still parses. A
  single JSON array would have to be rewritten whole on every append.
* Stored OUTSIDE the repo, under ~/Library/Application Support/debbiela/. The repo is
  on an iCloud-synced Desktop, which is what produced the EDEADLK in the first place —
  putting the recovery file next to the thing it recovers from would be circular.
* Pure functions taking a `send` callable, so the retry logic is testable without a
  network, without credentials and without Sheets.
"""
import json
import os
import tempfile

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                           "debbiela", "ledger")


def ledger_path(account, base_dir=None):
    """One file per account ('crypto', 'stock', ...)."""
    d = base_dir or DEFAULT_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{account}.jsonl")


def append_row(path, row):
    """Append one trade. Returns True if it is on disk, False if even this failed.

    Flushed and fsync'd: the whole point is surviving a crash seconds later, and a row
    sitting in the OS write buffer does not survive a power cut.
    """
    try:
        line = json.dumps(row, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def read_rows(path):
    """Every readable row. A corrupt line is SKIPPED, not fatal — one bad tail must not
    make the other 200 trades unreadable."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def pending_rows(path):
    return [r for r in read_rows(path) if not r.get("sent")]


def _rewrite(path, rows):
    """Replace the file atomically: write a temp file beside it, fsync, then rename.
    A crash mid-rewrite leaves the ORIGINAL intact rather than a half-file."""
    d = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def flush_pending(path, send, limit=50):
    """Retry unsent rows through `send(row) -> bool`. Returns (sent, still_pending).

    A row is marked sent ONLY when send() returns True. An exception from send is
    treated as a failure and the row stays pending — a Sheets outage must never consume
    the trade it failed to record.
    """
    rows = read_rows(path)
    if not rows:
        return 0, 0
    sent = 0
    tried = 0
    for r in rows:
        if r.get("sent"):
            continue
        if tried >= limit:
            break
        tried += 1
        try:
            ok = bool(send(r))
        except Exception:
            ok = False
        if ok:
            r["sent"] = True
            sent += 1
    if sent:
        _rewrite(path, rows)
    return sent, sum(1 for r in rows if not r.get("sent"))
