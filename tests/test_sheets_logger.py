from sheets_logger import build_macro_row, build_ledger_row, MACRO_HEADER, LEDGER_HEADER


def test_build_macro_row_shape_matches_header():
    row = build_macro_row("2026-07-12", 5001.49, 0.03, 64000.0, 1.2)
    assert len(row) == len(MACRO_HEADER)


def test_build_macro_row_values_and_rounding():
    row = build_macro_row("2026-07-12", 5001.4919, 0.02981, 64012.3456, -1.234567)
    assert row[0] == "2026-07-12"
    assert row[1] == 5001.49
    assert row[2] == 0.03
    assert row[3] == 64012.3456
    assert row[4] == -1.235


def test_build_ledger_row_shape_matches_header():
    row = build_ledger_row("2026-07-12T05:14:00+00:00", "AVAX", "LONG",
                            6.71, 6.686655, 35.3949, -1.22, "trend_follow LONG")
    assert len(row) == len(LEDGER_HEADER)


def test_build_ledger_row_values_and_rounding():
    row = build_ledger_row("2026-07-12T05:14:00+00:00", "AVAX", "LONG",
                            6.7100001, 6.6866549, 35.394932, -1.224999, "trend_follow LONG")
    assert row[0] == "2026-07-12T05:14:00+00:00"
    assert row[1] == "AVAX"
    assert row[2] == "LONG"
    assert row[3] == 6.7100
    assert row[4] == 6.686655
    assert row[5] == 35.394932
    assert row[6] == -1.22
    assert row[7] == "trend_follow LONG"


def test_build_ledger_row_short_side():
    row = build_ledger_row("2026-07-12T00:00:00+00:00", "BTC", "SHORT",
                            64000.0, 63500.0, 0.02, 10.0, "breakout_chase SHORT")
    assert row[2] == "SHORT"
    assert row[6] == 10.0
