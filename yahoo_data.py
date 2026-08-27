"""
Free, no-key intraday OHLC data for equities/ETFs via Yahoo Finance's
public chart endpoint. Same candle dict shape as backtest.fetch_klines
({time (ms), open, high, low, close}) so core_model.py doesn't care
which source it came from.

Real constraint: Yahoo caps intraday granularities well below a year
(15m tops out around 60 days; 1h goes back ~2 years). Equity/ETF
backtests will cover a much smaller window than the crypto ones did.
"""

import time
import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo's actual caps per interval, conservative (avoid over-requesting and getting truncated silently).
MAX_RANGE_BY_INTERVAL = {"15m": "60d", "1h": "730d", "1d": "10y"}


def fetch_yahoo_klines(symbol: str, interval: str, days: int) -> list:
    max_range = MAX_RANGE_BY_INTERVAL.get(interval, "60d")
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"interval": interval, "range": max_range},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("chart", {}).get("result")
    if not result:
        return []
    r = result[0]
    timestamps = r.get("timestamp", [])
    quote = r["indicators"]["quote"][0]

    candles = []
    for i, t in enumerate(timestamps):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"time": t * 1000, "open": o, "high": h, "low": l, "close": c})

    if days:
        cutoff = candles[-1]["time"] - days * 86400 * 1000 if candles else 0
        candles = [c for c in candles if c["time"] >= cutoff]

    time.sleep(0.3)  # be polite to the public endpoint
    return candles
