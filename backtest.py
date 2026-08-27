"""
Offline backtest for the OTE + STDV strategy (strategy.py), run through
the same risk-management gate the live bot uses (risk_manager.py), so
results reflect what the real system would have done — not just the
raw signal in isolation.

No account access, no live orders: pulls historical Binance klines
(free, public, no auth) and replays them bar by bar. A signal is only
acted on using the next bar's open (never the same bar's close, so the
backtest can't cheat by seeing the future), fills include configurable
fee + slippage assumptions, and each trade is walked forward until its
stop or target is hit (or a max holding period is reached).

Usage:
    python backtest.py --symbol DOGEUSDT --timeframe 15m --days 90
"""

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

from strategy import detect_ote_signal, OTE_LOW, OTE_HIGH
from risk_manager import (
    RiskManager,
    RISK_PER_TRADE_PCT,
    MAX_CONCURRENT_POSITIONS,
    DAILY_LOSS_CAP_PCT,
)

BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"

FEE_PCT = 0.001        # 0.1% per fill (Binance taker), applied on entry and exit
SLIPPAGE_PCT = 0.0005  # 0.05% adverse slippage, applied on entry and exit
MAX_HOLD_BARS = 96     # give up and exit at market if neither stop nor target hits


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str  # "target" | "stop" | "timeout"
    size: float
    risk_amount: float
    pnl: float
    r_multiple: float


def fetch_klines(symbol: str, interval: str, days: int) -> List[dict]:
    """Paginate Binance klines to cover `days` of history."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    candles = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for k in batch:
            candles.append({
                "time": k[0], "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
            })
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)  # be polite to the public endpoint
    return candles


def simulate(
    symbol: str,
    candles: List[dict],
    starting_equity: float,
    pivot_strength: int = 3,
    std_window: int = 20,
    min_std_multiple: float = 1.5,
):
    trades: List[Trade] = []
    state: dict = {}
    risk_manager = RiskManager(state)
    equity = starting_equity
    min_bars = std_window + pivot_strength * 2 + 1

    i = min_bars
    while i < len(candles) - 2:  # need bar i+1 to enter, at least bar i+2 to manage it
        window = candles[: i + 1]
        signal = detect_ote_signal(
            symbol, window, pivot_strength=pivot_strength,
            std_window=std_window, min_std_multiple=min_std_multiple,
        )
        if signal is None:
            i += 1
            continue

        entry_bar = candles[i + 1]
        entry_dt = datetime.fromtimestamp(entry_bar["time"] / 1000, tz=timezone.utc)

        decision = risk_manager.evaluate(signal, equity, now_dt=entry_dt)
        if not decision.approved:
            i += 1
            continue

        direction_sign = 1 if signal.direction == "long" else -1
        entry_price = entry_bar["open"] + entry_bar["open"] * SLIPPAGE_PCT * direction_sign
        entry_fee = entry_price * decision.position_size * FEE_PCT

        risk_manager.open_position(symbol, entry_price, signal.stop, decision.position_size)

        hold_end = min(len(candles), i + 1 + MAX_HOLD_BARS)
        exit_price = exit_reason = exit_dt = None
        last_j = i + 1
        for j in range(i + 2, hold_end):
            bar = candles[j]
            last_j = j
            if signal.direction == "long":
                hit_stop = bar["low"] <= signal.stop
                hit_target = bar["high"] >= signal.target_1
            else:
                hit_stop = bar["high"] >= signal.stop
                hit_target = bar["low"] <= signal.target_1

            if hit_stop:  # conservative: if both trigger in the same bar, assume the stop hit first
                exit_price, exit_reason = signal.stop, "stop"
            elif hit_target:
                exit_price, exit_reason = signal.target_1, "target"

            if exit_reason:
                exit_dt = datetime.fromtimestamp(bar["time"] / 1000, tz=timezone.utc)
                break

        if exit_reason is None:
            last_bar = candles[hold_end - 1]
            exit_price, exit_reason = last_bar["close"], "timeout"
            exit_dt = datetime.fromtimestamp(last_bar["time"] / 1000, tz=timezone.utc)
            last_j = hold_end - 1

        exit_price = exit_price - exit_price * SLIPPAGE_PCT * direction_sign
        exit_fee = exit_price * decision.position_size * FEE_PCT

        gross_pnl = (exit_price - entry_price) * decision.position_size * direction_sign
        pnl = gross_pnl - entry_fee - exit_fee
        equity += pnl

        risk_manager.close_position(symbol)
        risk_manager.record_fill_pnl(pnl, now_dt=exit_dt)

        trades.append(Trade(
            symbol=symbol, direction=signal.direction,
            entry_time=entry_dt.isoformat(), entry_price=entry_price,
            exit_time=exit_dt.isoformat(), exit_price=exit_price, exit_reason=exit_reason,
            size=decision.position_size, risk_amount=decision.risk_amount,
            pnl=pnl, r_multiple=(pnl / decision.risk_amount) if decision.risk_amount else 0.0,
        ))

        i = last_j + 1

    return trades, equity


def summarize(trades: List[Trade], starting_equity: float, ending_equity: float):
    if not trades:
        print("No trades were triggered by this strategy over the given period.")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_r = sum(t.r_multiple for t in trades) / len(trades)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    equity_curve = [starting_equity]
    running = starting_equity
    for t in trades:
        running += t.pnl
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
    print(
        f"Exit reasons:      target={sum(1 for t in trades if t.exit_reason == 'target')}  "
        f"stop={sum(1 for t in trades if t.exit_reason == 'stop')}  "
        f"timeout={sum(1 for t in trades if t.exit_reason == 'timeout')}"
    )


def main():
    parser = argparse.ArgumentParser(description="Backtest the OTE + STDV strategy against historical data.")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=10000.0)
    args = parser.parse_args()

    print(f"=== Backtest: {args.symbol} ({args.timeframe}, {args.days}d) ===")
    print(
        f"Strategy: OTE zone {OTE_LOW * 100:.1f}%-{OTE_HIGH * 100:.1f}% | "
        f"Risk gate: {RISK_PER_TRADE_PCT}%/trade, max {MAX_CONCURRENT_POSITIONS} concurrent, "
        f"{DAILY_LOSS_CAP_PCT}% daily loss cap | Fees {FEE_PCT * 100:.2f}%, slippage {SLIPPAGE_PCT * 100:.2f}% per fill\n"
    )

    print(f"Fetching {args.days}d of {args.timeframe} candles for {args.symbol}...")
    candles = fetch_klines(args.symbol, args.timeframe, args.days)
    print(f"Got {len(candles)} candles. Running simulation...\n")

    trades, ending_equity = simulate(args.symbol, candles, args.equity)
    summarize(trades, args.equity, ending_equity)


if __name__ == "__main__":
    main()
