"""
Backtest runner for core_model.py (Master Ruleset Part I).

Usage:
    python backtest_core_model.py --symbol DOGEUSDT --setup-tf 15m --htf 1h --days 90
"""

import argparse

from backtest import fetch_klines, FEE_PCT, SLIPPAGE_PCT
from core_model import simulate, summarize, to_candles


def main():
    parser = argparse.ArgumentParser(description="Backtest the OTE+STDV Master Ruleset core model.")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--setup-tf", default="15m")
    parser.add_argument("--htf", default="1h")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=10000.0)
    args = parser.parse_args()

    print(f"=== Core Model Backtest: {args.symbol} (setup={args.setup_tf}, HTF={args.htf}, {args.days}d) ===\n")

    print(f"Fetching {args.days}d of {args.setup_tf} setup candles...")
    setup_raw = fetch_klines(args.symbol, args.setup_tf, args.days)
    print(f"Fetching {args.days}d of {args.htf} HTF candles...")
    htf_raw = fetch_klines(args.symbol, args.htf, args.days)
    print(f"Got {len(setup_raw)} setup candles, {len(htf_raw)} HTF candles. Running simulation...\n")

    setup_candles = to_candles(setup_raw)
    htf_candles = to_candles(htf_raw)

    trades, ending_equity = simulate(
        args.symbol, setup_candles, htf_candles, args.setup_tf, args.htf,
        args.equity, FEE_PCT, SLIPPAGE_PCT,
    )
    summarize(trades, args.equity, ending_equity)
    return trades


if __name__ == "__main__":
    main()
