"""Shared news context for binance_bot.py (crypto) and tradingbot.py (stocks).

POLICY — news is CONTEXT FOR THE AI ONLY (user decision 2026-08-13). Fresh headlines
and a FinBERT sentiment read are handed to the Llama model that already gates every
entry, and it weighs them against structure. No hardcoded rule in here may block a
trade. This deliberately replaces the stock bot's previous direction-blind veto
(`confirm = not (negative and prob >= 0.60)`), which killed SHORTS on bad news —
backwards, since bad news argues FOR a short.

Source is Alpaca's news API, which a live check confirmed covers BOTH stocks and
crypto using the credentials already in config.py — no new key, no subscription.
(X/Twitter was considered and rejected: ~$100+/mo for usable read access, far noisier.)

FRESHNESS is the load-bearing part. That same live check found BTC carrying headlines
hours old while AVAX's newest was 6 DAYS old and market-wide rather than AVAX-specific.
Feeding that to the model as if it were current is worse than saying nothing, so stale
items are dropped and the prompt states plainly that there is no fresh news.

Every network path here is fail-soft: no key, no network, a bad response, or a slow
API all degrade to "no news" and the bots trade on structure exactly as before.
"""
from datetime import datetime, timezone

MAX_NEWS_AGE_HOURS = 24   # older than this is history, not a trading input
MAX_HEADLINES      = 6    # enough for context without swamping the prompt
NEWS_TIMEOUT_SECS  = 8    # never let a slow news API stall the trading loop


def _parse_ts(raw):
    """Parse an ISO-8601 timestamp, tolerating the trailing 'Z' the API returns.
    Returns an aware UTC datetime, or None if unparseable."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def filter_fresh_headlines(articles, now, max_age_hours=MAX_NEWS_AGE_HOURS):
    """[(headline, age_hours)] for articles inside the window, newest first.

    Malformed entries (missing/blank headline, missing or unparseable timestamp) are
    dropped rather than raised on — a single bad record from the feed must never take
    down a trading cycle."""
    if not articles:
        return []
    out = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        headline = (art.get("headline") or "").strip()
        if not headline:
            continue
        ts = _parse_ts(art.get("created_at") or art.get("updated_at"))
        if ts is None:
            continue
        age_hours = (now - ts).total_seconds() / 3600.0
        if age_hours < 0 or age_hours > max_age_hours:
            continue
        out.append((headline, age_hours))
    out.sort(key=lambda x: x[1])
    return out


def dedupe_headlines(items):
    """Drop repeats of the same story (case/whitespace-insensitive), keeping the
    freshest copy. Wire services re-run near-identical headlines constantly; six slots
    of the same story is six wasted slots."""
    seen, out = set(), []
    for headline, age in sorted(items, key=lambda x: x[1]):
        key = " ".join(headline.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append((headline, age))
    return out


def format_news_block(items, sentiment=None, probability=0.0,
                      max_age_hours=MAX_NEWS_AGE_HOURS):
    """The prompt fragment describing current news, or its absence.

    Deliberately states facts only — headlines, their age, and the sentiment read with
    its confidence. It gives the model no instruction about what to DO, because the
    moment this text starts saying "skip this trade" the AI-decides contract has
    quietly become a hardcoded rule. tests/test_news.py enforces that."""
    if not items:
        return (f"RECENT NEWS: no fresh news in the last {max_age_hours}h for this "
                f"symbol — judge the setup on structure alone; do not infer a news "
                f"narrative that isn't here.")

    # Ages FLOOR rather than round: a 7.5h-old item shown as "8h ago" overstates how
    # stale it is, and staleness is the thing this module exists to be honest about.
    lines = "\n".join(f"  - [{int(age)}h ago] {headline}" for headline, age in items)
    if sentiment and probability > 0:
        conf = f"{probability * 100:.0f}% confidence"
        if probability < 0.60:
            conf += " (LOW CONFIDENCE — weak signal, weigh lightly)"
        read = f"FinBERT read of these headlines: {sentiment.upper()} ({conf})."
    else:
        read = "FinBERT read: unavailable."
    return f"RECENT NEWS (last {max_age_hours}h):\n{lines}\n{read}"


def fetch_news_articles(symbol, api_key, api_secret, limit=10,
                        timeout=NEWS_TIMEOUT_SECS):
    """Raw Alpaca news articles for a symbol, or [] on any failure.

    `symbol` accepts either form — "TSLA", "BTC/USD", "BTCUSD"; the slash is stripped
    because the news endpoint wants the compact form (both were verified live).
    Fail-soft by construction: any exception degrades to "no news" so a news outage can
    never stop the bot from trading on structure."""
    if not api_key or not api_secret:
        return []
    try:
        import requests
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            params={"symbols": symbol.replace("/", ""), "limit": limit},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get("news", []) or []
    except Exception:
        return []


def get_news_context(symbol, api_key, api_secret, now=None,
                     max_age_hours=MAX_NEWS_AGE_HOURS, sentiment_fn=None):
    """(prompt_block, headlines, sentiment, probability) — the one call the bots make.

    sentiment_fn is injected (FinBERT's estimate_sentiment) so this module stays free
    of the heavyweight transformers import; if it's missing or throws, the block still
    renders with the headlines and just omits the sentiment read."""
    now = now or datetime.now(timezone.utc)
    articles = fetch_news_articles(symbol, api_key, api_secret)
    items = dedupe_headlines(filter_fresh_headlines(articles, now, max_age_hours))[:MAX_HEADLINES]
    headlines = [h for h, _ in items]

    sentiment, probability = None, 0.0
    if headlines and sentiment_fn is not None:
        try:
            probability, sentiment = sentiment_fn(headlines)
        except Exception:
            sentiment, probability = None, 0.0

    return format_news_block(items, sentiment, probability, max_age_hours), \
        headlines, sentiment, probability
