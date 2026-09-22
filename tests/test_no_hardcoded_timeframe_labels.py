"""No timeframe may be written into a string by hand in the crypto bot.

This bug has now been fixed four separate times, each time in one place, each time
leaving the others:

    tf_tag         said "4H" while the fetch asked for "6h"
    chart labels   drew a 6H FVG over 5m candles with nothing saying so
    the AI prompt  told the model "4H candles" above 6H bars, ten times
    the log        "🎯 AMD (PRIORITY): 4H low-sweep $0.2488 + daily BULLISH"

They share one cause: the timeframe is fetched from a variable and then described from
memory. Bybit is the only 4H source and it is geo-blocked from this machine, so the
description was reliably wrong — but it is NOT a constant "6h" either, because the 4H
path is real whenever Bybit is reachable. Nothing can be hardcoded in either direction.

So this test bans the literal outright in executable code. A timeframe reaching a human
or a model must come from htf_name / HTF / HTF_TIMEFRAME / LTF_TIMEFRAME /
BYBIT_HTF_TIMEFRAME — the same values handed to the fetch.

Comments and docstrings are exempt: prose explaining that Bybit serves 4H is not a label
and cannot drift from the data.
"""
import ast
import io
import re

import pytest

SRC = io.open("binance_bot.py", encoding="utf-8").read()
TREE = ast.parse(SRC)

# "4H"/"6h" as a timeframe in its own right. A preceding digit means it is part of
# another number ("24h"), and 1H/1h is unambiguous and fetched literally everywhere.
TIMEFRAME_LITERAL = re.compile(r"(?<![0-9])[46]\s?[Hh]\b")


def _docstring_line_numbers():
    out = set()
    for node in ast.walk(TREE):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d is None:
                continue
            first = node.body[0]
            out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def _getenv_default_line_numbers():
    """os.getenv("HTF_TIMEFRAME", "6h") — these literals ARE the source of truth the
    rest of the file must read, so they are the one place the value may be written."""
    out = set()
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"):
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant):
                    out.add(arg.lineno)
    return out


def _offending_string_constants():
    """Every string LITERAL in the AST that names a 4H/6H timeframe."""
    skip = _docstring_line_numbers() | _getenv_default_line_numbers()
    bad = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in skip:
                continue
            if TIMEFRAME_LITERAL.search(node.value):
                bad.append((node.lineno, node.value.strip()[:70]))
    return sorted(set(bad))


def test_the_parse_actually_sees_strings():
    """Guard the guard — a broken walk would pass this file vacuously."""
    consts = [n for n in ast.walk(TREE)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert len(consts) > 200, len(consts)


def test_no_executable_string_hardcodes_a_4h_or_6h_label():
    bad = _offending_string_constants()
    assert not bad, (
        "these strings name a timeframe by hand instead of reading the one that was "
        "fetched:\n" + "\n".join(f"  line {ln}: {txt!r}" for ln, txt in bad))


def test_the_timeframe_constants_are_still_the_single_source():
    for name in ("HTF_TIMEFRAME", "LTF_TIMEFRAME", "BYBIT_HTF_TIMEFRAME"):
        assert re.search(rf"^{name}\s*=\s*os\.getenv\(", SRC, re.M), name


def test_the_bybit_banner_and_the_bybit_fetch_read_the_same_constant():
    """They sat two lines apart with the literal written twice; the banner announced 4H
    candles and nothing guaranteed the fetch below asked for them."""
    assert 'print(f"HTF data:  Bybit public ({BYBIT_HTF_TIMEFRAME.upper()} candles)")' in SRC
    assert "htf_name  = BYBIT_HTF_TIMEFRAME" in SRC
    assert re.search(r"fetch_ohlcv_retry\(htf_exchange, bybit_sym, htf_name,", SRC)


def test_the_startup_banner_does_not_claim_an_htf_it_has_not_resolved_yet():
    """The banner prints ~20 lines BEFORE connect_htf_exchange() decides which frame is
    available, so a definite "HTF: 6h" there was unbackable — right only because Bybit
    happens to be blocked."""
    assert "HTF: {BYBIT_HTF_TIMEFRAME} or {HTF_TIMEFRAME} (resolved below)" in SRC


def test_the_fallback_banner_names_the_configured_frame():
    assert 'falling back to {HTF_TIMEFRAME.upper()} on main exchange' in SRC


def test_no_executable_string_names_an_exchange_the_bot_never_connects_to():
    """Same defect, different field. The startup banner announced "real Kraken data"
    while connect_exchange() builds ccxt.coinbase (or ccxt.binance with a key) — and it
    printed ~27 lines BEFORE that choice is made, so it could not have known either way.
    Kraken appears nowhere in the client code; it was simply never true."""
    skip = _docstring_line_numbers()
    bad = [(n.lineno, n.value.strip()[:70]) for n in ast.walk(TREE)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and n.lineno not in skip and re.search(r"kraken", n.value, re.I)]
    assert not bad, bad


def test_the_banner_defers_the_exchange_to_the_line_that_resolves_it():
    assert "PAPER TRADING (live market data)" in SRC
    assert 'print(f"Exchange: {mode}  |  Chart display: Coinbase")' in SRC


def test_no_variable_is_named_after_a_timeframe_it_may_not_be():
    """is_bos_4h / direction_4h held the HTF result whatever frame the HTF was — the
    name itself re-seeded the literal every time someone printed it."""
    names = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
    bad = sorted(n for n in names if re.search(r"_[46]h$", n))
    assert not bad, f"rename these after the role, not a frame they may not be: {bad}"
