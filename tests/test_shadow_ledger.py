"""Shadow mode: write down what the retest rule refuses, decide later.

Measured over 38 decided setups before this existed:

    RETESTED  n=33   61% at the zone  ->  15% entered at market
    NO RETEST n= 5  100% at the zone  ->  80% entered at market

87% of setups retest and entering those early is a disaster; the 13% that never come
back do run. Nothing visible at arm time separated them on n=5, so the rule stays strict
and the refusals get logged instead. A few weeks of that is a real sample.

The 100%-at-the-zone figure is circular — "price never returned" IS the winning move,
and the fill was unreachable anyway — which is exactly why this stores several candidate
entries rather than assuming a market fill.
"""
import pytest

from bot.shadow_ledger import candidate_entries, record_refusal, load, shadow_path


# ── the candidate fills ──────────────────────────────────────────────────────

def test_a_long_gets_fills_between_the_price_and_the_zone_top():
    """Zone sits BELOW price, so coming back means falling toward zone_hi."""
    e = candidate_entries(110.0, 100.0, 104.0, is_long=True)
    assert e["market"] == 110.0
    assert e["part50"] == pytest.approx(107.0)      # halfway from 110 to 104
    assert e["part25"] == pytest.approx(108.5)
    assert e["part50"] < e["part25"] < e["market"]


def test_a_short_gets_fills_between_the_price_and_the_zone_bottom():
    e = candidate_entries(90.0, 96.0, 100.0, is_long=False)
    assert e["market"] == 90.0
    assert e["part50"] == pytest.approx(93.0)
    assert e["market"] < e["part25"] < e["part50"]


def test_a_partial_fill_is_strictly_better_than_market_for_a_long():
    """The point of storing them: market was the 15% case, and it is the worst fill
    available. A shallow dip that never reaches the zone still fills part25/part50."""
    e = candidate_entries(110.0, 100.0, 104.0, is_long=True)
    assert e["part25"] < e["market"] and e["part50"] < e["part25"]


@pytest.mark.parametrize("bad", [(0, 100, 104), (-5, 100, 104), (110, 104, 100)])
def test_nonsense_geometry_yields_no_candidates(bad):
    assert candidate_entries(bad[0], bad[1], bad[2], is_long=True) == {}


@pytest.mark.parametrize("bad", [None, "x"])
def test_unreadable_input_yields_no_candidates(bad):
    assert candidate_entries(bad, 100.0, 104.0, is_long=True) == {}


# ── recording ────────────────────────────────────────────────────────────────

def test_a_refusal_round_trips(tmp_path):
    p = str(tmp_path / "s.jsonl")
    assert record_refusal("DOGE/USD", "LONG", 0.0954, 0.0951, 0.0955,
                          stop=0.0930, target=0.1009, ts="2026-09-24T14:44:00Z", path=p)
    rows = load(p)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "DOGE/USD" and r["reason"] == "no_retest"
    assert set(r["entries"]) == {"market", "part25", "part50"}


def test_refusals_accumulate(tmp_path):
    p = str(tmp_path / "s.jsonl")
    for i in range(3):
        record_refusal(f"S{i}/USD", "LONG", 10.0, 9.0, 9.5, 8.0, 12.0, ts=i, path=p)
    assert len(load(p)) == 3


def test_extra_fields_ride_along(tmp_path):
    """So a future discriminator can be tested against data already collected."""
    p = str(tmp_path / "s.jsonl")
    record_refusal("X/USD", "LONG", 10.0, 9.0, 9.5, 8.0, 12.0, ts=0, path=p,
                   extra={"body_atr": 2.4, "bos_tf": "6h"})
    assert load(p)[0]["body_atr"] == 2.4


def test_recording_never_raises_into_the_entry_path(tmp_path):
    """This is observation, not a trading decision. It must not be able to break the
    thing it is observing."""
    assert record_refusal(None, None, None, None, None, None, None, None,
                          path=str(tmp_path / "s.jsonl")) in (True, False)


def test_an_unwritable_path_is_reported_not_raised():
    assert record_refusal("X/USD", "LONG", 10.0, 9.0, 9.5, 8.0, 12.0, ts=0,
                          path="/nope/nope/s.jsonl") is False


def test_the_shadow_file_is_separate_from_the_real_ledger():
    assert "shadow" in shadow_path()
    assert "Desktop" not in shadow_path()
