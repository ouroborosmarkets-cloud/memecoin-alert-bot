"""
OTE + STDV Master Ruleset — Part I (core model) only.

Implements raid -> displacement (+FVG) -> market structure shift ->
dual-leg fib (OTE entry off the displacement leg, STDV targets off the
raid leg) -> stop -> partial take-profit ladder -> minimum R:R gate,
per Part I of the ruleset. Part II modules (volatility-adaptive entry
laddering, Unicorn/CE/IFVG/SMT, ADR exhaustion, cycle timing, PO3,
grading) are deliberately NOT implemented yet — agreed build order is
core model first, backtest it clean, then add modules one at a time
and keep only the ones that measurably help.

Disclosed adaptations for crypto (continuous, 24/7, no bid/ask feed):
  - Session/macro time filters (Part I S3) are not applied — crypto has
    no single "primary session" the way FX/futures/equities do.
  - "Bias" and "Context" timeframes (Part I S2 table) are merged into
    one HTF role; "spread" (Part I S5) is approximated with a fixed
    proxy since klines carry no bid/ask.
  - Named liquidity pools are limited to prior-day/prior-week high/low
    (relative equal highs/lows clustering is deferred — Part II-ish
    scope creep for a first pass).
  - The S6 FVG "runner trail" after TP2 is not implemented; TP3 simply
    closes the remainder at its STDV level.
"""

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional

OTE_LOW = 0.618
OTE_HIGH = 0.79
RESTING_ENTRY_LEVEL = 0.705

DISPLACEMENT_ATR_MULT = 1.5
DISPLACEMENT_MAX_CANDLES = 3
MSS_SCAN_MAX_CANDLES = 10
MSS_PIVOT_STRENGTH = 2
STOP_ATR_MULT = 0.10
SPREAD_PROXY_PCT = 0.0005  # stand-in for "2 x current spread" (no bid/ask in klines)
TIME_STOP_MIN_R = 0.5
MIN_RR_TO_TP1 = 2.0
M_ORIGIN_LOOKBACK = 12
HTF_PIVOT_STRENGTH = 2
HTF_STRUCTURE_LOOKBACK_BARS = 12  # "last 12 hours" when HTF = 1h
MIN_DRAW_DISTANCE_PCT = 0.003  # a pool this close to price is an imminent raid target, not a standing "draw"


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class CoreSignal:
    symbol: str
    direction: Literal["long", "short"]
    price: float          # entry fill price (0.705 fib)
    stop: float
    tp1: float
    tp2: float
    tp3: float
    a_far: float
    a_near: float
    m_origin: float
    stdev: float           # "1 STDV" of the raid leg
    raid_index: int
    mss_index: int
    fill_index: int


def to_candles(raw: List[dict]) -> List[Candle]:
    return [Candle(r["time"], r["open"], r["high"], r["low"], r["close"]) for r in raw]


def atr(candles: List[Candle], end: int, period: int = 14) -> Optional[float]:
    """ATR(period) using candles[end-period:end] (end exclusive, no lookahead)."""
    if end - period < 1:
        return None
    trs = []
    for i in range(end - period, end):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return sum(trs) / len(trs)


def find_pivots(candles: List[Candle], strength: int):
    pivot_highs, pivot_lows = [], []
    n = len(candles)
    for i in range(strength, n - strength):
        window = candles[i - strength : i + strength + 1]
        if candles[i].high == max(c.high for c in window):
            pivot_highs.append(i)
        if candles[i].low == min(c.low for c in window):
            pivot_lows.append(i)
    return pivot_highs, pivot_lows


def most_recent_confirmed(pivot_indices: List[int], cutoff: int, strength: int) -> Optional[int]:
    """Latest pivot index that was already confirmed (index+strength < cutoff) — no lookahead."""
    knowable = [p for p in pivot_indices if p + strength < cutoff]
    return knowable[-1] if knowable else None


def precompute_daily_weekly(candles: List[Candle]):
    """For every index i, the prior UTC calendar day's/week's high & low, using only
    candles strictly before i's own day/week (rolling reference levels, not fixed
    structures — recomputed once here so per-bar lookups are O(1))."""
    n = len(candles)
    dts = [datetime.fromtimestamp(c.time / 1000, tz=timezone.utc) for c in candles]
    day_keys = [d.date() for d in dts]
    week_keys = [d.isocalendar()[:2] for d in dts]

    day_hi: Dict = {}
    day_lo: Dict = {}
    week_hi: Dict = {}
    week_lo: Dict = {}
    for i, c in enumerate(candles):
        dk, wk = day_keys[i], week_keys[i]
        day_hi[dk] = max(day_hi.get(dk, c.high), c.high)
        day_lo[dk] = min(day_lo.get(dk, c.low), c.low)
        week_hi[wk] = max(week_hi.get(wk, c.high), c.high)
        week_lo[wk] = min(week_lo.get(wk, c.low), c.low)

    all_days = sorted(day_hi.keys())
    all_weeks = sorted(week_hi.keys())

    prior_day_high = [None] * n
    prior_day_low = [None] * n
    prior_week_high = [None] * n
    prior_week_low = [None] * n
    for i in range(n):
        pd = day_keys[i] - timedelta(days=1)
        if pd in day_hi:
            prior_day_high[i], prior_day_low[i] = day_hi[pd], day_lo[pd]
        pw_date = dts[i] - timedelta(days=7)
        pw = pw_date.isocalendar()[:2]
        if pw in week_hi:
            prior_week_high[i], prior_week_low[i] = week_hi[pw], week_lo[pw]

    return prior_day_high, prior_day_low, prior_week_high, prior_week_low, day_keys, week_keys


def tf_to_minutes(tf: str) -> int:
    unit = tf[-1]
    n = int(tf[:-1])
    return n if unit == "m" else n * 60 if unit == "h" else n * 1440


def check_bias(
    htf: List[Candle],
    htf_cutoff: int,
    current_price: float,
    htf_pivot_highs: List[int],
    htf_pivot_lows: List[int],
    htf_prior_day_high, htf_prior_day_low, htf_prior_week_high, htf_prior_week_low,
    htf_idx: int,
) -> Optional[Literal["long", "short"]]:
    ph_idx = most_recent_confirmed(htf_pivot_highs, htf_cutoff, HTF_PIVOT_STRENGTH)
    pl_idx = most_recent_confirmed(htf_pivot_lows, htf_cutoff, HTF_PIVOT_STRENGTH)
    if ph_idx is None or pl_idx is None:
        return None
    range_high, range_low = htf[ph_idx].high, htf[pl_idx].low
    if not (range_low < current_price < range_high):
        return None
    equilibrium = (range_high + range_low) / 2
    bias1 = "long" if current_price < equilibrium else "short"

    pools_above, pools_below = [], []
    for price in (htf_prior_day_high[htf_idx], htf_prior_week_high[htf_idx]):
        if price is not None and price > current_price * (1 + MIN_DRAW_DISTANCE_PCT):
            pools_above.append(price)
    for price in (htf_prior_day_low[htf_idx], htf_prior_week_low[htf_idx]):
        if price is not None and price < current_price * (1 - MIN_DRAW_DISTANCE_PCT):
            pools_below.append(price)
    if not pools_above and not pools_below:
        return None
    dist_above = min(pools_above) - current_price if pools_above else float("inf")
    dist_below = current_price - max(pools_below) if pools_below else float("inf")
    bias2 = "long" if dist_above < dist_below else "short"

    window_start = max(0, htf_cutoff - HTF_STRUCTURE_LOOKBACK_BARS)
    recent_lows = [p for p in htf_pivot_lows if window_start <= p < htf_cutoff and p + HTF_PIVOT_STRENGTH < htf_cutoff]
    recent_highs = [p for p in htf_pivot_highs if window_start <= p < htf_cutoff and p + HTF_PIVOT_STRENGTH < htf_cutoff]
    bias3 = None
    if len(recent_lows) >= 2 and htf[recent_lows[-1]].low > htf[recent_lows[-2]].low:
        bias3 = "long"
    elif len(recent_highs) >= 2 and htf[recent_highs[-1]].high < htf[recent_highs[-2]].high:
        bias3 = "short"

    if bias1 == bias2 == bias3:
        return bias1
    return None


def simulate(
    symbol: str,
    setup_candles: List[Candle],
    htf_candles: List[Candle],
    setup_tf: str,
    htf_tf: str,
    starting_equity: float,
    fee_pct: float,
    slippage_pct: float,
    min_rr_to_tp1: float = MIN_RR_TO_TP1,
    no_chase_stdv_mult: float = 1.0,
    displacement_atr_mult: float = DISPLACEMENT_ATR_MULT,
):
    from risk_manager import RiskManager

    n = len(setup_candles)
    setup_min = tf_to_minutes(setup_tf)
    bars_90min = max(1, round(90 / setup_min))
    bars_45min = max(1, round(45 / setup_min))

    setup_pivot_highs, setup_pivot_lows = find_pivots(setup_candles, MSS_PIVOT_STRENGTH)
    htf_pivot_highs, htf_pivot_lows = find_pivots(htf_candles, HTF_PIVOT_STRENGTH)

    s_pdh, s_pdl, s_pwh, s_pwl, day_keys, week_keys = precompute_daily_weekly(setup_candles)
    h_pdh, h_pdl, h_pwh, h_pwl, _, _ = precompute_daily_weekly(htf_candles)

    htf_times = [c.time for c in htf_candles]

    def htf_index_for(t: int) -> Optional[int]:
        idx = bisect.bisect_right(htf_times, t) - 1
        return idx if idx >= 0 else None

    blacklist: Dict = {}  # (day_key, pool_name) -> True, or (week_key, pool_name) -> True

    trades = []
    state: dict = {}
    risk_manager = RiskManager(state)
    equity = starting_equity

    min_start = max(50, M_ORIGIN_LOOKBACK + 5)
    i = min_start
    while i < n - DISPLACEMENT_MAX_CANDLES - MSS_SCAN_MAX_CANDLES - bars_90min - 2:
        hi = htf_index_for(setup_candles[i].time)
        if hi is None or hi < 5:
            i += 1
            continue

        bias = check_bias(
            htf_candles, hi, setup_candles[i - 1].close,
            htf_pivot_highs, htf_pivot_lows,
            h_pdh, h_pdl, h_pwh, h_pwl, hi,
        )
        if bias is None:
            i += 1
            continue

        dk, wk = day_keys[i], week_keys[i]
        candidates = []
        if bias == "long":
            if s_pdl[i] is not None and not blacklist.get((dk, "day_low")) and setup_candles[i].low < s_pdl[i] <= setup_candles[i].close:
                candidates.append(("day_low", s_pdl[i], dk))
            if s_pwl[i] is not None and not blacklist.get((wk, "week_low")) and setup_candles[i].low < s_pwl[i] <= setup_candles[i].close:
                candidates.append(("week_low", s_pwl[i], wk))
        else:
            if s_pdh[i] is not None and not blacklist.get((dk, "day_high")) and setup_candles[i].high > s_pdh[i] >= setup_candles[i].close:
                candidates.append(("day_high", s_pdh[i], dk))
            if s_pwh[i] is not None and not blacklist.get((wk, "week_high")) and setup_candles[i].high > s_pwh[i] >= setup_candles[i].close:
                candidates.append(("week_high", s_pwh[i], wk))

        if not candidates:
            i += 1
            continue

        # prefer week-grade pool if both raided this bar
        candidates.sort(key=lambda c: 0 if "week" in c[0] else 1)
        pool_name, pool_price, pool_key = candidates[0]
        raid_index = i
        a_far = setup_candles[i].low if bias == "long" else setup_candles[i].high

        # --- displacement + FVG, within 3 candles of the raid ---
        disp_index = None
        for d in range(raid_index + 1, min(n - 1, raid_index + 1 + DISPLACEMENT_MAX_CANDLES)):
            a = atr(setup_candles, d, 14)
            if a is None:
                continue
            rng = setup_candles[d].high - setup_candles[d].low
            if rng < displacement_atr_mult * a:
                continue
            c1, c3 = setup_candles[d - 1], setup_candles[d + 1]
            if bias == "long" and c1.high < c3.low:
                disp_index = d
                break
            if bias == "short" and c1.low > c3.high:
                disp_index = d
                break

        if disp_index is None:
            i = raid_index + 1
            continue

        # --- market structure shift: body close through most recent confirmed opposing pivot ---
        mss_index = None
        for m in range(disp_index, min(n, disp_index + MSS_SCAN_MAX_CANDLES)):
            if bias == "long":
                p = most_recent_confirmed(setup_pivot_highs, raid_index, MSS_PIVOT_STRENGTH)
                if p is not None and setup_candles[m].close > setup_candles[p].high:
                    mss_index = m
                    break
            else:
                p = most_recent_confirmed(setup_pivot_lows, raid_index, MSS_PIVOT_STRENGTH)
                if p is not None and setup_candles[m].close < setup_candles[p].low:
                    mss_index = m
                    break

        if mss_index is None:
            i = disp_index + 1
            continue

        # --- Fib A (OTE, displacement leg) ---
        leg = setup_candles[raid_index : mss_index + 1]
        a_near = max(c.high for c in leg) if bias == "long" else min(c.low for c in leg)
        leg_range = abs(a_near - a_far)
        if leg_range <= 0:
            i = mss_index + 1
            continue
        if bias == "long":
            zone_high = a_near - OTE_LOW * leg_range   # 0.62 level
            zone_low = a_near - OTE_HIGH * leg_range    # 0.79 level
            entry_level = a_near - RESTING_ENTRY_LEVEL * leg_range
        else:
            zone_low = a_near + OTE_LOW * leg_range
            zone_high = a_near + OTE_HIGH * leg_range
            entry_level = a_near + RESTING_ENTRY_LEVEL * leg_range

        # --- Fib B (STDV, raid leg) ---
        if raid_index - M_ORIGIN_LOOKBACK < 0:
            i = mss_index + 1
            continue
        pre_raid = setup_candles[raid_index - M_ORIGIN_LOOKBACK : raid_index]
        m_origin = max(c.high for c in pre_raid) if bias == "long" else min(c.low for c in pre_raid)
        stdev = abs(m_origin - a_far)
        if stdev <= 0:
            i = mss_index + 1
            continue

        def stdv_target(k: float) -> float:
            return m_origin + k * stdev if bias == "long" else m_origin - k * stdev

        tp1 = a_near
        tp2_raw = stdv_target(2.0)
        tp3_raw = stdv_target(2.5)
        # liquidity cap: nearest opposing named pool overrides an STDV level beyond it
        opposing_pools = [p for p in (s_pdh[i], s_pwh[i]) if p is not None and p > entry_level] if bias == "long" \
            else [p for p in (s_pdl[i], s_pwl[i]) if p is not None and p < entry_level]
        if opposing_pools:
            cap = min(opposing_pools) if bias == "long" else max(opposing_pools)
            tp2 = min(tp2_raw, cap) if bias == "long" else max(tp2_raw, cap)
            tp3 = min(tp3_raw, cap) if bias == "long" else max(tp3_raw, cap)
        else:
            tp2, tp3 = tp2_raw, tp3_raw

        # --- entry: resting limit at 0.705, scan forward with cancel conditions ---
        entered_zone = False
        fill_index = None
        cancel = False
        scan_end = min(n - 1, mss_index + bars_90min)
        for f in range(mss_index + 1, scan_end + 1):
            bar = setup_candles[f]
            beyond_far = bar.low < a_far if bias == "long" else bar.high > a_far
            if beyond_far:
                blacklist[(pool_key, pool_name)] = True
                cancel = True
                break
            touched_zone = (bar.low <= zone_high) if bias == "long" else (bar.high >= zone_high)
            if touched_zone:
                entered_zone = True
            reached_t1 = (bar.high >= stdv_target(no_chase_stdv_mult)) if bias == "long" else (bar.low <= stdv_target(no_chase_stdv_mult))
            if reached_t1 and not entered_zone:
                cancel = True
                break
            touched_entry = (bar.low <= entry_level) if bias == "long" else (bar.high >= entry_level)
            if touched_entry:
                closed_beyond_79 = (bar.close < zone_low) if bias == "long" else (bar.close > zone_low)
                if closed_beyond_79:
                    cancel = True
                    break
                fill_index = f
                break
        else:
            cancel = True

        if cancel or fill_index is None:
            i = scan_end + 1
            continue

        # --- R:R gate ---
        entry_price_raw = entry_level
        buf = max(STOP_ATR_MULT * (atr(setup_candles, fill_index, 14) or 0), 2 * entry_price_raw * SPREAD_PROXY_PCT)
        stop = a_far - buf if bias == "long" else a_far + buf
        risk_per_unit = abs(entry_price_raw - stop)
        reward_to_tp1 = abs(tp1 - entry_price_raw)
        if risk_per_unit <= 0 or reward_to_tp1 / risk_per_unit < min_rr_to_tp1:
            i = fill_index + 1
            continue

        fill_dt = datetime.fromtimestamp(setup_candles[fill_index].time / 1000, tz=timezone.utc)

        class _Sig:
            pass

        sig = _Sig()
        sig.symbol, sig.direction, sig.price, sig.stop = symbol, bias, entry_price_raw, stop

        decision = risk_manager.evaluate(sig, equity, now_dt=fill_dt)
        if not decision.approved:
            i = fill_index + 1
            continue

        direction_sign = 1 if bias == "long" else -1
        entry_price = entry_price_raw + entry_price_raw * slippage_pct * direction_sign
        size = decision.position_size
        entry_fee = entry_price * size * fee_pct

        risk_manager.open_position(symbol, entry_price, stop, size)

        remaining = size
        current_stop = stop
        tp1_hit = tp2_hit = False
        realized_pnl = -entry_fee
        exit_reasons = []
        bars_since_fill = 0
        closed = False

        for j in range(fill_index + 1, n):
            bar = setup_candles[j]
            bars_since_fill += 1

            stopped = (bar.low <= current_stop) if bias == "long" else (bar.high >= current_stop)
            if stopped:
                px = current_stop - current_stop * slippage_pct * direction_sign
                realized_pnl += (px - entry_price) * direction_sign * remaining - px * remaining * fee_pct
                exit_reasons.append("stop" if current_stop == stop else "stop_be")
                remaining = 0
                closed = True
                break

            if bars_since_fill >= bars_45min and not tp1_hit:
                unrealized_r = (bar.close - entry_price) * direction_sign / risk_per_unit
                if unrealized_r < TIME_STOP_MIN_R:
                    px = bar.close - bar.close * slippage_pct * direction_sign
                    realized_pnl += (px - entry_price) * direction_sign * remaining - px * remaining * fee_pct
                    exit_reasons.append("time_stop")
                    remaining = 0
                    closed = True
                    break

            hit_tp1 = (bar.high >= tp1) if bias == "long" else (bar.low <= tp1)
            if hit_tp1 and not tp1_hit:
                px = tp1 - tp1 * slippage_pct * direction_sign
                qty = size * 0.4
                realized_pnl += (px - entry_price) * direction_sign * qty - px * qty * fee_pct
                remaining -= qty
                current_stop = entry_price
                tp1_hit = True
                exit_reasons.append("tp1")

            hit_tp2 = (bar.high >= tp2) if bias == "long" else (bar.low <= tp2)
            if tp1_hit and hit_tp2 and not tp2_hit:
                px = tp2 - tp2 * slippage_pct * direction_sign
                qty = size * 0.4
                realized_pnl += (px - entry_price) * direction_sign * qty - px * qty * fee_pct
                remaining -= qty
                tp2_hit = True
                exit_reasons.append("tp2")

            hit_tp3 = (bar.high >= tp3) if bias == "long" else (bar.low <= tp3)
            if tp2_hit and hit_tp3:
                px = tp3 - tp3 * slippage_pct * direction_sign
                realized_pnl += (px - entry_price) * direction_sign * remaining - px * remaining * fee_pct
                exit_reasons.append("tp3")
                remaining = 0
                closed = True
                break

        if not closed:
            # ran out of data — mark to last close
            last = setup_candles[-1]
            px = last.close
            realized_pnl += (px - entry_price) * direction_sign * remaining - px * remaining * fee_pct
            exit_reasons.append("data_end")
            j = n - 1

        equity += realized_pnl
        risk_manager.close_position(symbol)
        exit_dt = datetime.fromtimestamp(setup_candles[j].time / 1000, tz=timezone.utc)
        risk_manager.record_fill_pnl(realized_pnl, now_dt=exit_dt)

        trades.append({
            "symbol": symbol, "direction": bias,
            "entry_time": fill_dt.isoformat(), "entry_price": entry_price,
            "exit_time": exit_dt.isoformat(), "pnl": realized_pnl,
            "risk_amount": decision.risk_amount,
            "r_multiple": realized_pnl / decision.risk_amount if decision.risk_amount else 0.0,
            "exit_reasons": exit_reasons,
        })

        i = j + 1

    return trades, equity


def summarize(trades, starting_equity, ending_equity):
    if not trades:
        print("No trades were triggered by this model over the given period.")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    equity_curve = [starting_equity]
    running = starting_equity
    for t in trades:
        running += t["pnl"]
        equity_curve.append(running)
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    print(f"Trades:            {len(trades)}")
    print(f"Win rate:          {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg R multiple:    {avg_r:+.2f}R")
    print(f"Profit factor:     {profit_factor:.2f}")
    print(f"Starting equity:   ${starting_equity:,.2f}")
    print(f"Ending equity:     ${ending_equity:,.2f}  ({(ending_equity / starting_equity - 1) * 100:+.2f}%)")
    print(f"Max drawdown:      {max_dd:.2f}%")
