"""
Backtest runner for core_model.py (Master Ruleset Part I).

Usage:
    python backtest_core_model.py --symbol DOGEUSDT --source crypto --setup-tf 15m --htf 1h --days 90
    python backtest_core_model.py --symbol TSLA --source equity --setup-tf 15m --htf 1h --days 60
    python backtest_core_model.py --symbols TSLA,NVDA,GME,AMC --source equity --setup-tf 15m --htf 1h
"""

import argparse

from backtest import fetch_klines, FEE_PCT, SLIPPAGE_PCT
from yahoo_data import fetch_yahoo_klines
from core_model import simulate, summarize, to_candles

# Equities have real bid/ask spreads and exchange fees/commissions differ
# from crypto; Robinhood equity trades are commission-free, so fee_pct=0
# is the realistic default there. Slippage assumption kept the same.
EQUITY_FEE_PCT = 0.0


def fetch(symbol: str, source: str, tf: str, days: int) -> list:
    if source == "crypto":
        return fetch_klines(symbol, tf, days)
    return fetch_yahoo_klines(symbol, tf, days)


def run_one(symbol: str, source: str, setup_tf: str, htf_tf: str, days: int, equity: float,
            min_rr: float, no_chase_mult: float):
    print(f"=== Core Model Backtest: {symbol} ({source}, setup={setup_tf}, HTF={htf_tf}, {days}d, "
          f"min_rr={min_rr}, no_chase_mult={no_chase_mult}) ===\n")

    print(f"Fetching setup candles...")
    setup_raw = fetch(symbol, source, setup_tf, days)
    print(f"Fetching HTF candles...")
    htf_raw = fetch(symbol, source, htf_tf, days)
    print(f"Got {len(setup_raw)} setup candles, {len(htf_raw)} HTF candles. Running simulation...\n")

    if len(setup_raw) < 100 or len(htf_raw) < 20:
        print("Not enough data returned — skipping.\n")
        return [], equity

    setup_candles = to_candles(setup_raw)
    htf_candles = to_candles(htf_raw)

    fee_pct = FEE_PCT if source == "crypto" else EQUITY_FEE_PCT
    trades, ending_equity = simulate(
        symbol, setup_candles, htf_candles, setup_tf, htf_tf,
        equity, fee_pct, SLIPPAGE_PCT, min_rr, no_chase_mult,
    )
    summarize(trades, equity, ending_equity)
    print()
    return trades, ending_equity


def main():
    parser = argparse.ArgumentParser(description="Backtest the OTE+STDV Master Ruleset core model.")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated list, run each independently.")
    parser.add_argument("--source", default="crypto", choices=["crypto", "equity"])
    parser.add_argument("--setup-tf", default="15m")
    parser.add_argument("--htf", default="1h")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--equity", type=float, default=10000.0)
    parser.add_argument("--min-rr", type=float, default=2.0, help="R:R gate to TP1 (spec default 2.0)")
    parser.add_argument("--no-chase-mult", type=float, default=1.0,
                         help="STDV multiple for the no-chase cancel rule (spec default 1.0)")
    args = parser.parse_args()

    default_days = 90 if args.source == "crypto" else 60
    days = args.days or default_days

    symbols = args.symbols.split(",") if args.symbols else [args.symbol or "DOGEUSDT"]
    for sym in symbols:
        run_one(sym.strip(), args.source, args.setup_tf, args.htf, days, args.equity,
                args.min_rr, args.no_chase_mult)


if __name__ == "__main__":
    main()
