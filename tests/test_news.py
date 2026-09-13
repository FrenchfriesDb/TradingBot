"""News-context pipeline shared by binance_bot.py and tradingbot.py.

Policy (user decision 2026-08-13): news is CONTEXT FOR THE AI ONLY. Headlines and a
FinBERT sentiment read are handed to the model that already gates every entry; no
hardcoded news rule may override technicals. This replaces the stock bot's old
direction-blind veto (`confirm = not (negative and prob >= 0.60)`), which blocked
SHORTS on bad news — exactly backwards, since bad news is an argument FOR a short.

Freshness matters more than it looks: a live check of Alpaca's news API showed BTC
carrying headlines hours old while AVAX's newest was 6 DAYS old and market-wide
rather than AVAX-specific. Gating a trade on that is worse than having no news at
all, so stale items are dropped and the model is told plainly that there is none.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.news import (
    filter_fresh_headlines, dedupe_headlines, format_news_block, MAX_NEWS_AGE_HOURS,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _article(headline, hours_ago, ts_key="created_at"):
    return {"headline": headline, ts_key: (NOW - timedelta(hours=hours_ago)).isoformat()}


# ─────────────────────── filter_fresh_headlines ───────────────────────
def test_keeps_recent_headlines_newest_first():
    arts = [_article("older but fresh", 5), _article("newest", 1)]
    out = filter_fresh_headlines(arts, NOW, max_age_hours=24)
    assert [h for h, _ in out] == ["newest", "older but fresh"]
    assert out[0][1] == pytest.approx(1.0)


def test_drops_stale_headlines():
    # the real AVAX case: newest item was ~6 days old
    arts = [_article("Why Wall Street's Crypto Bet Is Just Getting Started", 24 * 6)]
    assert filter_fresh_headlines(arts, NOW, max_age_hours=24) == []


def test_boundary_is_inclusive_at_the_cutoff():
    assert len(filter_fresh_headlines([_article("exactly at cutoff", 24)], NOW, 24)) == 1
    assert len(filter_fresh_headlines([_article("just past cutoff", 25)], NOW, 24)) == 0


def test_ignores_malformed_and_empty_entries():
    arts = [
        {"headline": "no timestamp"},
        {"created_at": NOW.isoformat()},                 # no headline
        {"headline": "bad ts", "created_at": "not-a-date"},
        {"headline": "   ", "created_at": NOW.isoformat()},   # whitespace only
        _article("good one", 2),
    ]
    out = filter_fresh_headlines(arts, NOW, max_age_hours=24)
    assert [h for h, _ in out] == ["good one"]


def test_handles_zulu_timestamps_from_the_real_api():
    # Alpaca returns e.g. "2026-08-13T04:58:00Z"
    arts = [{"headline": "zulu", "created_at": (NOW - timedelta(hours=3))
             .isoformat().replace("+00:00", "Z")}]
    out = filter_fresh_headlines(arts, NOW, max_age_hours=24)
    assert [h for h, _ in out] == ["zulu"]


def test_empty_input_is_safe():
    assert filter_fresh_headlines([], NOW, 24) == []
    assert filter_fresh_headlines(None, NOW, 24) == []


# ─────────────────────────── dedupe_headlines ───────────────────────────
def test_dedupes_case_and_whitespace_insensitively_keeping_order():
    items = [("Bitcoin Rallies", 1.0), ("bitcoin   rallies", 2.0), ("Fed Holds Rates", 3.0)]
    assert [h for h, _ in dedupe_headlines(items)] == ["Bitcoin Rallies", "Fed Holds Rates"]


def test_dedupe_keeps_the_freshest_copy():
    items = [("Same Story", 1.0), ("same story", 9.0)]
    out = dedupe_headlines(items)
    assert len(out) == 1 and out[0][1] == 1.0


# ─────────────────────────── format_news_block ───────────────────────────
def test_block_states_plainly_when_there_is_no_fresh_news():
    block = format_news_block([], sentiment=None, probability=0.0)
    assert "no fresh news" in block.lower()
    # The model must not be nudged into inventing a narrative it doesn't have.
    assert "structure alone" in block.lower()


def test_block_lists_headlines_with_their_age_and_sentiment():
    block = format_news_block([("Fed cuts rates", 2.0), ("ETF inflows surge", 7.5)],
                              sentiment="positive", probability=0.83)
    assert "Fed cuts rates" in block and "ETF inflows surge" in block
    assert "2h ago" in block and "7h ago" in block
    assert "POSITIVE" in block and "83" in block


def test_block_marks_sentiment_as_low_confidence_when_it_is():
    block = format_news_block([("Mixed signals in crypto", 1.0)],
                              sentiment="neutral", probability=0.41)
    assert "low confidence" in block.lower()


def test_block_never_tells_the_model_what_to_do():
    # Policy guard: news is CONTEXT. If someone later slips a directive in here, the
    # "AI decides" contract quietly becomes a hardcoded rule — fail loudly instead.
    block = format_news_block([("Company files for bankruptcy", 1.0)],
                              sentiment="negative", probability=0.95)
    lowered = block.lower()
    for banned in ("do not take", "skip this", "you must", "reject the trade", "veto"):
        assert banned not in lowered


def test_default_cutoff_is_a_day():
    assert MAX_NEWS_AGE_HOURS == 24
