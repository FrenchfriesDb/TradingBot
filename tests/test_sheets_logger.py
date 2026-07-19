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
    row = build_ledger_row("2026-07-12T05:00:00+00:00", "2026-07-12T05:14:00+00:00", "AVAX",
                            "LONG", 6.71, 6.60, 6.90, 6.686655, 35.3949, 59.28, 248.61, 4,
                            -1.22, "trend_follow LONG")
    assert len(row) == len(LEDGER_HEADER)


def test_build_ledger_row_values_and_rounding():
    row = build_ledger_row("2026-07-12T05:00:00+00:00", "2026-07-12T05:14:00+00:00", "AVAX",
                            "LONG", 6.7100001, 6.60, 6.90, 6.6866549, 35.394932, 59.2849,
                            248.6099, 4, -1.224999, "trend_follow LONG")
    assert row[0] == "2026-07-12T05:00:00+00:00"
    assert row[1] == "2026-07-12T05:14:00+00:00"
    assert row[2] == "AVAX"
    assert row[3] == "LONG"
    assert row[4] == 6.7100
    assert row[5] == 6.60
    assert row[6] == 6.90
    assert row[7] == 6.686655
    assert row[8] == 35.394932
    assert row[9] == 59.28
    assert row[10] == 248.61
    assert row[11] == 4
    assert row[12] == -1.22
    assert row[13] == "trend_follow LONG"
    assert row[14] == ""  # no chart_url passed -> empty cell, not a formula


def test_build_ledger_row_short_side():
    row = build_ledger_row("2026-07-12T00:00:00+00:00", "2026-07-12T01:00:00+00:00", "BTC",
                            "SHORT", 64000.0, 64500.0, 63000.0, 63500.0, 0.02, 320.0, 1280.0,
                            4, 10.0, "breakout_chase SHORT")
    assert row[3] == "SHORT"
    assert row[12] == 10.0


def test_build_ledger_row_chart_url_becomes_image_formula():
    row = build_ledger_row("2026-07-12T00:00:00+00:00", "2026-07-12T01:00:00+00:00", "BTC",
                            "SHORT", 64000.0, 64500.0, 63000.0, 63500.0, 0.02, 320.0, 1280.0,
                            4, 10.0, "breakout_chase SHORT",
                            chart_url="https://drive.google.com/uc?export=view&id=abc123")
    u = "https://drive.google.com/uc?export=view&id=abc123"
    assert row[14] == f'=HYPERLINK("{u}", IMAGE("{u}"))'


def test_build_ledger_row_local_chart_path_is_plain_text():
    row = build_ledger_row("2026-07-12T00:00:00+00:00", "2026-07-12T01:00:00+00:00", "BTC",
                            "SHORT", 64000.0, 64500.0, 63000.0, 63500.0, 0.02, 320.0, 1280.0,
                            4, 10.0, "breakout_chase SHORT",
                            chart_url="charts/BTC_20260712T010000.png")
    assert row[14] == "charts/BTC_20260712T010000.png"  # not wrapped in IMAGE() — not a URL
