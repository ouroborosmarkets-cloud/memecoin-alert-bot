"""
Risk-management gate between a detected OTE signal and any order
placement. Nothing here talks to a broker — it only decides whether a
signal is allowed to be traded and, if so, what size, given the
account's current equity and open-position/PnL state.

Guardrails (user-approved):
  - Risk per trade:  1.25% of account equity
  - Max concurrent:  1 open position at a time
  - Daily loss cap:  3.75% of equity -> new trades halt for the rest
                      of the UTC day once hit
  - Stop-loss:        mandatory; a signal with no stop is rejected

`state` here is the same dict bot.py persists to state.json, so the
gate's memory (open positions, today's realized PnL) survives restarts.
Once real order execution exists, the executor is expected to call
open_position() / close_position() / record_fill_pnl() so these
guardrails stay accurate — right now nothing calls them because no
orders are placed.
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.25"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))
DAILY_LOSS_CAP_PCT = float(os.getenv("DAILY_LOSS_CAP_PCT", "3.75"))


@dataclass
class TradeDecision:
    approved: bool
    reason: str
    position_size: Optional[float] = None
    risk_amount: Optional[float] = None


class RiskManager:
    def __init__(self, state: dict):
        self.state = state
        state.setdefault(
            "risk",
            {"open_positions": {}, "daily_pnl": 0.0, "daily_pnl_date": None},
        )

    def _roll_day(self, now_dt: Optional[datetime] = None):
        now_dt = now_dt or datetime.now(timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")
        risk = self.state["risk"]
        if risk["daily_pnl_date"] != today:
            risk["daily_pnl_date"] = today
            risk["daily_pnl"] = 0.0

    def record_fill_pnl(self, realized_pnl: float, now_dt: Optional[datetime] = None):
        self._roll_day(now_dt)
        self.state["risk"]["daily_pnl"] += realized_pnl

    def open_position(self, symbol: str, entry: float, stop: float, size: float):
        self.state["risk"]["open_positions"][symbol] = {
            "entry": entry, "stop": stop, "size": size, "opened_at": time.time(),
        }

    def close_position(self, symbol: str):
        self.state["risk"]["open_positions"].pop(symbol, None)

    def evaluate(self, signal, account_equity: float, now_dt: Optional[datetime] = None,
                 available_cash: Optional[float] = None) -> TradeDecision:
        self._roll_day(now_dt)
        risk = self.state["risk"]

        if signal.stop is None:
            return TradeDecision(False, "no stop-loss on signal — mandatory hard stop required, rejecting")

        if signal.symbol in risk["open_positions"]:
            return TradeDecision(False, f"{signal.symbol} already has an open position")

        if len(risk["open_positions"]) >= MAX_CONCURRENT_POSITIONS:
            return TradeDecision(False, f"max concurrent positions ({MAX_CONCURRENT_POSITIONS}) already open")

        if account_equity <= 0:
            return TradeDecision(False, "no account equity known — cannot size position")

        daily_loss_pct = max(0.0, -risk["daily_pnl"]) / account_equity * 100
        if daily_loss_pct >= DAILY_LOSS_CAP_PCT:
            return TradeDecision(
                False,
                f"daily loss cap hit ({daily_loss_pct:.2f}% >= {DAILY_LOSS_CAP_PCT}%) — halted until tomorrow (UTC)",
            )

        per_unit_risk = abs(signal.price - signal.stop)
        if per_unit_risk <= 0:
            return TradeDecision(False, "invalid stop distance (zero) — rejecting")

        risk_amount = account_equity * (RISK_PER_TRADE_PCT / 100)
        position_size = risk_amount / per_unit_risk

        # Risk-based sizing can ask for more than the account can actually pay for
        # (a tight stop on a small account is the common case) — cap by cash on hand.
        cash = account_equity if available_cash is None else available_cash
        max_affordable_size = cash / signal.price if signal.price > 0 else 0
        if position_size > max_affordable_size:
            position_size = max_affordable_size
            risk_amount = position_size * per_unit_risk
            if position_size <= 0:
                return TradeDecision(False, "not enough cash on hand to afford any position at this entry price")

        return TradeDecision(True, "approved", position_size=position_size, risk_amount=risk_amount)
