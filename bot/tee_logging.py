"""Make a bot write a file log NO MATTER how it was launched.

scripts/start_*.sh redirects stdout into logs/<name>.log, so a supervised bot is always
recorded. A bot started by hand in a terminal is not — its output goes to the tty and
vanishes with the window.

That is not a tidiness complaint. It has blocked three separate live diagnoses:

  2026-09-01  ASTER — "why did it enter?"  no record
  2026-09-04  POL   — same question         no record
  2026-09-05  ASTER — a fill 4.19% outside its own armed zone, path unknown, because
                      the reasoning went to /dev/ttys003

Each time the answer existed for a few minutes of scrollback and then was gone. The bot
prints exactly what it needs to explain itself; it just wasn't durable.

So the bot tees its own stdout, rather than relying on how it was invoked. Teeing is
skipped when stdout is already a file — a script launch has redirected it, and teeing
there would write every line twice into the same log.
"""
import os
import sys


class _Tee:
    """Write to the terminal and a file at once, flushing both."""

    def __init__(self, stream, handle):
        self._stream = stream
        self._handle = handle

    def write(self, data):
        self._stream.write(data)
        try:
            self._handle.write(data)
            # Flush every line. A bare launch has no PYTHONUNBUFFERED, and an unflushed
            # buffer is indistinguishable from a hung bot when you tail the file — which
            # has itself caused a healthy bot to be killed and restarted.
            self._handle.flush()
        except Exception:
            pass                      # a full disk must never take the bot down
        return len(data)

    def flush(self):
        self._stream.flush()
        try:
            self._handle.flush()
        except Exception:
            pass

    def isatty(self):
        return self._stream.isatty()

    def fileno(self):
        return self._stream.fileno()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")


def should_tee(stream):
    """True when this stream is a terminal, so its output would otherwise be lost.

    A script launch has already redirected stdout to the log file; teeing on top of that
    duplicates every line. Anything that cannot be inspected (a stub, a closed handle) is
    left alone rather than guessed at.
    """
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def tee_stdout_to(path, stream=None, opener=open):
    """Tee stdout into `path` when it would otherwise only reach a terminal.

    Returns the path actually being written, or None when teeing was skipped or failed.
    Appends — restarts are frequent and the history is the point. Idempotent: teeing an
    already-teed stream is a no-op, so calling this twice cannot double every line.
    """
    target = stream if stream is not None else sys.stdout
    if isinstance(target, _Tee):
        return getattr(target._handle, "name", None)
    if not should_tee(target):
        return None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handle = opener(path, "a", encoding="utf-8")
    except Exception:
        return None                   # logging is never worth failing a startup over
    tee = _Tee(target, handle)
    if stream is None:
        sys.stdout = tee
        # stderr only when it points at the same terminal, so a tracebacks-only redirect
        # set up by the operator is left exactly as they arranged it.
        try:
            if should_tee(sys.stderr) and sys.stderr.fileno() == target.fileno():
                sys.stderr = _Tee(sys.stderr, handle)
        except Exception:
            pass
    return path
