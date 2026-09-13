"""A bare terminal launch must still produce a file log.

Three live diagnoses have failed because the bot's reasoning went to a tty and vanished
(ASTER 2026-09-01, POL 2026-09-04, ASTER 2026-09-05). scripts/start_*.sh redirects stdout
to logs/, but a hand-started bot had no such luck. Now the bot tees its own output.
"""
import io
import os

from bot.tee_logging import _Tee, should_tee, tee_stdout_to


class _Tty(io.StringIO):
    def isatty(self): return True
    def fileno(self): return 1


class _File(io.StringIO):
    def isatty(self): return False
    def fileno(self): return 7


def test_a_terminal_gets_teed():
    assert should_tee(_Tty()) is True


def test_a_redirected_file_is_left_alone():
    """A script launch already writes the log; teeing would duplicate every line."""
    assert should_tee(_File()) is False


def test_an_uninspectable_stream_is_left_alone():
    class Broken:
        def isatty(self): raise OSError("no")
    assert should_tee(Broken()) is False


def test_output_reaches_both_destinations():
    term, log = _Tty(), io.StringIO()
    tee = _Tee(term, log)
    tee.write("[ASTER] entry refused\n")
    assert term.getvalue() == "[ASTER] entry refused\n"
    assert log.getvalue() == "[ASTER] entry refused\n"


def test_a_failing_log_never_breaks_the_bot():
    """A full disk must not take down a live trading loop."""
    class Dead(io.StringIO):
        def write(self, _): raise OSError("disk full")
    term = _Tty()
    _Tee(term, Dead()).write("still trading\n")
    assert term.getvalue() == "still trading\n"


def test_skipped_when_stdout_is_already_a_file(tmp_path):
    p = str(tmp_path / "x.log")
    assert tee_stdout_to(p, stream=_File()) is None
    assert not os.path.exists(p)


def test_teeing_is_idempotent(tmp_path):
    """Calling twice must not double every line."""
    p = str(tmp_path / "x.log")
    log = io.StringIO(); log.name = p
    already = _Tee(_Tty(), log)
    assert tee_stdout_to(p, stream=already) == p


def test_appends_rather_than_truncating(tmp_path):
    p = tmp_path / "bot.log"
    p.write_text("earlier session\n", encoding="utf-8")
    opened = {}
    def _opener(path, mode, encoding=None):
        opened["mode"] = mode
        return io.StringIO()
    tee_stdout_to(str(p), stream=_Tty(), opener=_opener)
    assert opened["mode"] == "a", "restarts are frequent; history is the point"
    assert p.read_text(encoding="utf-8") == "earlier session\n"


def test_unwritable_path_returns_none_instead_of_raising(tmp_path):
    def _boom(*a, **k): raise PermissionError("nope")
    assert tee_stdout_to(str(tmp_path / "x.log"), stream=_Tty(), opener=_boom) is None
