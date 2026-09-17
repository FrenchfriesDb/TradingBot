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


# ── the wiring, not just the mechanism ────────────────────────────────────────
#
# Everything above tests tee_stdout_to() itself. None of it tested whether any bot
# actually CALLS it — and for two weeks tradingbot.py did not. The function worked
# perfectly and the stock bot still wrote no file log at all, because the wiring was
# only ever added to binance_bot.py. On 2026-09-17 a bare-tty `tradingbot.py live`
# (PID 90132, fds 1 and 2 both on /dev/ttys002) was confirmed writing nowhere, four
# hours of DNS-outage diagnosis surviving only in terminal scrollback.
#
# So the wiring is the thing under test here: called at all, with the right log name,
# early enough to catch the startup banner, and guarded so importing the module in a
# test suite never tees.
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# a call before these land is a call before the bot can print anything interesting
HEAVY = ("lumibot", "ccxt", "pandas", "config", "sheets_logger", "chart_renderer",
         "bot.strategy", "bot.crypto_strategy")


def _tee_calls(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == "tee_stdout_to"]


def _first_heavy_import_line(tree):
    lines = []
    for n in ast.walk(tree):
        mod = None
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
        elif isinstance(n, ast.Import):
            mod = n.names[0].name
        if mod and any(mod == h or mod.startswith(h + ".") for h in HEAVY):
            lines.append(n.lineno)
    return min(lines) if lines else None


@pytest.mark.parametrize("script,logname", [
    ("binance_bot.py", "binance_bot.log"),
    ("tradingbot.py", "stock_bot.log"),
])
def test_each_bot_tees_its_own_stdout(script, logname):
    tree = ast.parse((REPO / script).read_text(encoding="utf-8"))
    calls = _tee_calls(tree)
    assert calls, f"{script} never calls tee_stdout_to — a bare launch would log nowhere"

    consts = [c.value for call in calls for c in ast.walk(call)
              if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert logname in consts, f"{script} tees somewhere other than logs/{logname}: {consts}"
    assert "logs" in consts, f"{script} does not tee into the logs/ directory"

    heavy = _first_heavy_import_line(tree)
    if heavy is not None:
        assert calls[0].lineno < heavy, (
            f"{script} tees at line {calls[0].lineno}, after its first heavy import at "
            f"line {heavy} — the startup banner would be lost")


@pytest.mark.parametrize("script", ["binance_bot.py", "tradingbot.py"])
def test_teeing_is_guarded_so_importing_the_module_never_tees(script):
    """pytest imports these modules; an unguarded tee would spray the suite into a log."""
    tree = ast.parse((REPO / script).read_text(encoding="utf-8"))
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "__name__"):
            guarded.update(c.lineno for b in node.body for c in _tee_calls(ast.Module(
                body=[b], type_ignores=[])))
    calls = _tee_calls(tree)
    assert calls, f"{script} never calls tee_stdout_to"
    for call in calls:
        assert call.lineno in guarded, (
            f'{script}:{call.lineno} tees outside an `if __name__ == "__main__"` guard')
