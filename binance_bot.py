import time
import math
import ccxt
import pandas as pd
from datetime import datetime

from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET,
    BINANCE_TESTNET,
    BINANCE_SYMBOL,
    BINANCE_CASH_AT_RISK,
    TIMEFRAME_LTF,
    SLEEP_TIME,
)
from bot import indicators
from finbert_utils import estimate_sentiment

# Simple Binance testnet runner using CCXT
# WARNING: This is an example adapter for spot trading on Binance testnet.


def connect_binance(api_key, secret, testnet=True):
    exchange = ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'},
    })
    if testnet:
        exchange.set_sandbox_mode(True)
    return exchange


def ohlcv_to_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df


def position_sizing(balance_quote, last_price, risk_fraction):
    # risk in quote currency (e.g., USDT)
    risk_amount = balance_quote * risk_fraction
    if last_price <= 0:
        return 0
    qty = risk_amount / last_price
    # round down to sensible precision
    return qty


def fetch_balance_quote(exchange, quote_currency='USDT'):
    bal = exchange.fetch_balance()
    free = bal.get('free', {})
    return float(free.get(quote_currency, 0.0))


def run_once():
    exchange = connect_binance(BINANCE_API_KEY, BINANCE_SECRET, BINANCE_TESTNET)
    symbol = BINANCE_SYMBOL
    timeframe = TIMEFRAME_LTF

    # fetch 100 candles
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
    df = ohlcv_to_df(ohlcv)

    # indicators expect numeric columns
    # 1) Sweep detection
    sweep, support_level, sweep_wick_low = indicators.check_liquidity_sweep(df)
    mss, swing_high = indicators.check_market_structure_shift(df)
    fvg_bull, fvg_bottom, fvg_top = indicators.find_bullish_fvg(df)

    last_price = float(df['close'].iloc[-1])
    print(f"{datetime.now()} {symbol} last={last_price:.2f} sweep={sweep} mss={mss} fvg={fvg_bull}")

    # If we detect the Debbie-La setup (bullish) then consider entry
    if sweep and mss and fvg_bull:
        # fetch quote balance (USDT)
        balance_quote = fetch_balance_quote(exchange, quote_currency='USDT')
        qty = position_sizing(balance_quote, last_price, BINANCE_CASH_AT_RISK)

        if qty <= 0:
            print("No funds available to size position.")
            return

        # Sentiment check
        try:
            news = []
            probability, sentiment = estimate_sentiment(news)
        except Exception:
            probability, sentiment = 0.0, 'neutral'

        if sentiment == 'positive' and probability >= 0.5:
            # Place market buy on testnet
            print(f"Placing market buy: qty={qty:.6f} {symbol.split('/')[0]} @ {last_price:.2f}")
            try:
                order = exchange.create_order(symbol, 'market', 'buy', qty)
                print('Order placed:', order)
            except Exception as e:
                print('Order error:', e)
        else:
            print('Sentiment not confirmed, skipping entry.')
    else:
        print('No valid setup detected.')


if __name__ == '__main__':
    print('Starting single-run Binance testnet check. For continuous mode, wrap in a loop.')
    run_once()
