"""Every closed trade must record WHY it closed, in a form you can group on.

Until now the ledger's "Reason" column held the strategy that OPENED the trade
("BOS LONG", "manipulation_down LONG") and nothing recorded the exit. So a run of
near-zero rows was unreadable: four consecutive trades on 2026-09-04 all exited within
a few dollars of entry and there was no way to tell target from stop from timeout
without replaying candles.

THE SUBTLETY THAT MAKES THIS WORTH DOING PROPERLY: there is no separate break-even exit
path. manage_open_trade trails `stop_loss` up to break-even at +1.25R, and the position
then closes through the ordinary stop branch labelled "SL hit". So break-even exits and
real stop-outs are the SAME string, and only `state.breakeven_moved` separates them.

That distinction is the entire point. On 121 real trades the split was:

    TARGET     +$609.57      the thesis working
    STALE      +$184.15      the 6h timer earning its keep
    BREAKEVEN  -$228.35      costs paid, no move captured  <- the fixable leak
    STOP       -$580.53      the cost of being wrong

Collapsing BREAKEVEN into STOP hides the one line item that is actually addressable.
"""
import pytest

from bot.indicators import normalize_exit_reason
from sheets_logger import LEDGER_HEADER, build_ledger_row


# ── the five real strings the bot produces ──
def test_five_min_loop_target():
    assert normalize_exit_reason("🟢 TP hit") == "TARGET"


def test_five_min_loop_stop():
    assert normalize_exit_reason("🔴 SL hit") == "STOP"


def test_five_min_loop_stale():
    assert normalize_exit_reason("⏰ Stale 6.0h") == "STALE"


def test_watcher_labels():
    assert normalize_exit_reason("🟢 TP HIT (watcher)") == "TARGET"
    assert normalize_exit_reason("🔴 SL HIT (watcher)") == "STOP"


def test_startup_catchup_labels():
    assert normalize_exit_reason("🟢 TP HIT (startup catch-up — bot was offline)") == "TARGET"
    assert normalize_exit_reason("🔴 SL HIT (startup catch-up — bot was offline)") == "STOP"


# ── the break-even split ──
def test_stop_after_the_breakeven_trail_is_not_a_stop_out():
    """The string is identical; only breakeven_moved tells them apart."""
    assert normalize_exit_reason("🔴 SL hit", breakeven_moved=False) == "STOP"
    assert normalize_exit_reason("🔴 SL hit", breakeven_moved=True) == "BREAKEVEN"


def test_a_winner_that_trailed_then_ran_to_target_is_still_a_target():
    """breakeven_moved is True for most winners — it must not swallow TARGET."""
    assert normalize_exit_reason("🟢 TP hit", breakeven_moved=True) == "TARGET"


def test_timeout_outranks_the_breakeven_flag():
    """A position that trailed to break-even and then timed out exited on the CLOCK."""
    assert normalize_exit_reason("⏰ Stale 6.2h", breakeven_moved=True) == "STALE"


# ── robustness: this runs after the position is already gone ──
@pytest.mark.parametrize("raw", [None, "", "something unexpected", 42])
def test_unknown_input_degrades_to_other_rather_than_raising(raw):
    assert normalize_exit_reason(raw) == "OTHER"


def test_case_and_spacing_insensitive():
    assert normalize_exit_reason("  sl HIT  ") == "STOP"
    assert normalize_exit_reason("tp Hit") == "TARGET"


def test_tokens_are_a_closed_set():
    """The column is meant to be grouped/pivoted, so the vocabulary must stay small."""
    allowed = {"TARGET", "STOP", "BREAKEVEN", "STALE", "OTHER"}
    samples = ["🟢 TP hit", "🔴 SL hit", "⏰ Stale 6.0h", "🟢 TP HIT (watcher)",
               "🔴 SL HIT (startup catch-up — bot was offline)", "", None, "weird"]
    assert {normalize_exit_reason(s) for s in samples} <= allowed


# ── the ledger row ──
def test_exit_reason_column_exists_and_is_appended_last():
    """Appended for the same reason as Fees: ~177 historical rows have their chart in
    column O, and inserting ahead of Chart would shift every new row's chart."""
    assert LEDGER_HEADER[-1] == "Exit Reason"
    assert LEDGER_HEADER.index("Chart") == 14
    assert LEDGER_HEADER.index("Fees ($)") == 15


def test_row_carries_the_exit_reason():
    row = build_ledger_row("t0", "t1", "POL", "LONG", 0.09465, 0.09332, 0.10721,
                           0.09487, 14368.07672, 135.99, 1359.94, 10, -3.65,
                           "BOS LONG", None, fees=6.81, exit_reason="STALE")
    assert len(row) == len(LEDGER_HEADER)
    assert row[LEDGER_HEADER.index("Exit Reason")] == "STALE"
    assert row[LEDGER_HEADER.index("Reason")] == "BOS LONG", "opening strategy is untouched"


def test_defaults_keep_untouched_callers_working():
    row = build_ledger_row("t0", "t1", "POL", "LONG", 1, 1, 1, 1, 1, 1, 1, 10, 0, "r")
    assert len(row) == len(LEDGER_HEADER)
    assert row[-1] == ""


# ─────────────────────────────────────────────────────────────
# broker-side closes: inferred from the fill, not a label
# ─────────────────────────────────────────────────────────────

from bot.indicators import infer_exit_reason


def test_fill_at_the_target_leg():
    assert infer_exit_reason(110.0, stop_loss=95.0, take_profit=110.0) == "TARGET"


def test_fill_at_the_stop_leg():
    assert infer_exit_reason(95.0, stop_loss=95.0, take_profit=110.0) == "STOP"


def test_stop_leg_after_the_trail_is_a_breakeven():
    assert infer_exit_reason(95.0, 95.0, 110.0, breakeven_moved=True) == "BREAKEVEN"


def test_slight_slippage_past_a_leg_still_counts():
    """Real fills overshoot; 0.2% of the span is inside tolerance."""
    assert infer_exit_reason(94.98, stop_loss=95.0, take_profit=110.0) == "STOP"
    assert infer_exit_reason(110.02, stop_loss=95.0, take_profit=110.0) == "TARGET"


def test_a_fill_nowhere_near_either_leg_is_not_guessed():
    """An EOD flatten or stale exit lands mid-range. Forcing it into STOP/TARGET would
    poison the grouping this column exists for."""
    assert infer_exit_reason(102.0, stop_loss=95.0, take_profit=110.0) == "OTHER"


def test_degenerate_levels_do_not_divide_by_zero():
    assert infer_exit_reason(100.0, 100.0, 100.0) == "OTHER"
    assert infer_exit_reason(100.0, None, 110.0) == "OTHER"


# ─────────────────────────────────────────────────────────────
# backfilling headings on a tab that already exists
# ─────────────────────────────────────────────────────────────

from sheets_logger import _col_letter, missing_header_cells


def test_column_letters():
    assert [_col_letter(i) for i in (0, 14, 15, 16, 25)] == ["A", "O", "P", "Q", "Z"]
    assert _col_letter(26) == "AA"


def test_live_15_column_ledger_gets_both_new_headings():
    """The real case: a tab created before Fees and Exit Reason existed."""
    live = LEDGER_HEADER[:15]
    rng, values = missing_header_cells(live, LEDGER_HEADER)
    assert rng == "P1:Q1"
    assert values == [["Fees ($)", "Exit Reason"]]


def test_nothing_to_do_when_already_current():
    assert missing_header_cells(LEDGER_HEADER, LEDGER_HEADER) is None


def test_never_rewrites_an_existing_label():
    """Someone may have renamed a heading by hand. Only cells PAST the end are filled."""
    renamed = ["MY OWN NAME"] + LEDGER_HEADER[1:15]
    rng, values = missing_header_cells(renamed, LEDGER_HEADER)
    assert rng == "P1:Q1"
    assert "MY OWN NAME" not in values[0]


def test_empty_or_missing_header_row_fills_everything():
    rng, values = missing_header_cells([], LEDGER_HEADER)
    assert rng == f"A1:{_col_letter(len(LEDGER_HEADER) - 1)}1"
    assert values == [LEDGER_HEADER]
    assert missing_header_cells(None, LEDGER_HEADER)[1] == [LEDGER_HEADER]
