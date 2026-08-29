"""
OTE (Optimal Trade Entry) + STDV swing strategy.

ICT-style setup: find the most recent swing low/high pivot pair (a
confirmed "leg"), filter out legs that are small relative to recent
price volatility (stdev of closes) so we don't treat chop as a real
move, then flag when price retraces back into the classic 61.8%-79%
zone of that leg.

Signal-only. This module never places orders — it returns a candidate
setup (entry zone, invalidation stop, stdev-based targets) for the
caller to alert on or act on.
"""

import statistics
from dataclasses import dataclass
from typing import List, Literal, Optional, TypedDict

OTE_LOW = 0.618
OTE_HIGH = 0.79


class Candle(TypedDict):
    high: float
    low: float
    close: float


@dataclass
class Pivot:
    index: int
    price: float


@dataclass
class OTESignal:
    symbol: str
    direction: Literal["long", "short"]
    price: float
    zone_low: float
    zone_high: float
    swing_low: float
    swing_high: float
    stdev: float
    stop: float
    target_1: float
    target_2: float


def find_pivots(candles: List[Candle], strength: int = 3):
    """Fractal-style pivots: a bar is a pivot high/low if it's the
    max/min among `strength` bars on each side of it."""
    pivot_highs, pivot_lows = [], []
    n = len(candles)
    for i in range(strength, n - strength):
        window = candles[i - strength : i + strength + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            pivot_highs.append(Pivot(i, candles[i]["high"]))
        if candles[i]["low"] == min(c["low"] for c in window):
            pivot_lows.append(Pivot(i, candles[i]["low"]))
    return pivot_highs, pivot_lows


def detect_ote_signal(
    symbol: str,
    candles: List[Candle],
    pivot_strength: int = 3,
    std_window: int = 20,
    min_std_multiple: float = 1.5,
    target_multiple: float = 1.0,
) -> Optional[OTESignal]:
    if len(candles) < std_window + pivot_strength * 2 + 1:
        return None

    pivot_highs, pivot_lows = find_pivots(candles, pivot_strength)
    if not pivot_highs or not pivot_lows:
        return None

    last_high = pivot_highs[-1]
    last_low = pivot_lows[-1]

    closes = [c["close"] for c in candles[-std_window:]]
    stdev = statistics.pstdev(closes)
    if stdev == 0:
        return None

    high, low = last_high.price, last_low.price
    swing_range = high - low
    if swing_range <= 0 or swing_range < min_std_multiple * stdev:
        return None  # leg too small relative to volatility — likely noise

    price = candles[-1]["close"]

    if last_high.index > last_low.index:
        # low -> high impulse: bullish leg, look for a long on the retrace down
        direction: Literal["long", "short"] = "long"
        zone_low = high - OTE_HIGH * swing_range
        zone_high = high - OTE_LOW * swing_range
        stop = low - 0.25 * stdev
        target_1 = price + target_multiple * stdev
        target_2 = price + 2 * target_multiple * stdev
    else:
        # high -> low impulse: bearish leg, look for a short on the retrace up
        direction = "short"
        zone_low = low + OTE_LOW * swing_range
        zone_high = low + OTE_HIGH * swing_range
        stop = high + 0.25 * stdev
        target_1 = price - target_multiple * stdev
        target_2 = price - 2 * target_multiple * stdev

    if not (zone_low <= price <= zone_high):
        return None

    return OTESignal(
        symbol=symbol,
        direction=direction,
        price=price,
        zone_low=zone_low,
        zone_high=zone_high,
        swing_low=low,
        swing_high=high,
        stdev=stdev,
        stop=stop,
        target_1=target_1,
        target_2=target_2,
    )
