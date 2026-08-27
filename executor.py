"""
Order construction + risk-gate wiring for the Robinhood Agentic account.

This module builds the exact order this bot WOULD submit given an
approved signal — it does not talk to Robinhood itself. Actually
submitting an order requires the Robinhood MCP tools, which are only
reachable from an interactive Claude session with that connector
authenticated — never from a standalone script. submit_order() below
is a deliberate stub that raises until that wiring is done with the
real tool schema in hand (never guessed at) and reviewed live.

Hard safety rail: every function here refuses to operate on any
account other than the one Robinhood marked agentic_allowed=true for
this connection ("Agentic", account number 599103884). This is
enforced in code, not just by convention — see assert_agentic_account.
"""

from dataclasses import dataclass
from typing import List, Literal

from risk_manager import RiskManager, TradeDecision

AGENTIC_ACCOUNT_NUMBER = "599103884"
AGENTIC_RHS_ACCOUNT_NUMBER = "599103884"  # same account; crypto calls take the rhs_ field


class WrongAccountError(Exception):
    """Raised if any code path tries to touch an account other than the Agentic one."""


def assert_agentic_account(account_number: str):
    if account_number not in (AGENTIC_ACCOUNT_NUMBER, AGENTIC_RHS_ACCOUNT_NUMBER):
        raise WrongAccountError(
            f"Refusing to build/submit an order for account {account_number!r} — "
            f"this bot is only authorized to trade the Agentic account "
            f"({AGENTIC_ACCOUNT_NUMBER})."
        )


@dataclass
class TradeIntent:
    symbol: str  # internal symbol, e.g. "DOGEUSDT" or "TSLA"
    asset_class: Literal["crypto", "equity"]
    direction: Literal["long", "short"]
    entry: float
    stop: float
    targets: List[float]  # ordered, e.g. [tp1, tp2, tp3]


@dataclass
class OrderPlan:
    account_number: str
    asset_class: Literal["crypto", "equity"]
    broker_symbol: str
    entry_side: Literal["buy", "sell"]
    exit_side: Literal["buy", "sell"]
    quantity: float
    entry_order_type: Literal["limit"]
    entry_limit_price: float
    stop_price: float
    risk_amount: float
    targets: List[float]


def crypto_symbol_to_robinhood(symbol: str) -> str:
    """'DOGEUSDT' -> 'DOGE-USD' (Robinhood crypto pairs are quoted against USD)."""
    base = symbol.upper()
    for suffix in ("USDT", "USD"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}-USD"


def build_order(intent: TradeIntent, decision: TradeDecision) -> OrderPlan:
    if not decision.approved:
        raise ValueError(f"Cannot build an order from a rejected signal: {decision.reason}")
    if intent.direction == "short":
        raise NotImplementedError(
            "Short entries are not wired up — the Agentic account is a limited-margin "
            "account and its short-selling permissions haven't been verified. Check "
            "that before ever building a short order; long-only for now."
        )

    account_number = AGENTIC_ACCOUNT_NUMBER
    assert_agentic_account(account_number)

    broker_symbol = crypto_symbol_to_robinhood(intent.symbol) if intent.asset_class == "crypto" else intent.symbol

    return OrderPlan(
        account_number=account_number,
        asset_class=intent.asset_class,
        broker_symbol=broker_symbol,
        entry_side="buy",
        exit_side="sell",
        quantity=decision.position_size,
        entry_order_type="limit",
        entry_limit_price=intent.entry,
        stop_price=intent.stop,
        risk_amount=decision.risk_amount,
        targets=intent.targets,
    )


def plan_trade(intent: TradeIntent, state: dict, account_equity: float) -> OrderPlan:
    """Run a trade intent through the risk gate and build the order plan if approved.
    Raises ValueError if the risk gate rejects it."""

    class _Sig:
        pass

    sig = _Sig()
    sig.symbol, sig.direction, sig.price, sig.stop = intent.symbol, intent.direction, intent.entry, intent.stop

    risk_manager = RiskManager(state)
    decision = risk_manager.evaluate(sig, account_equity)
    if not decision.approved:
        raise ValueError(f"Signal rejected by risk gate: {decision.reason}")
    return build_order(intent, decision)


def submit_order(plan: OrderPlan):
    """
    Deliberately not implemented. Submitting a real order requires, in order:
      1. The Robinhood MCP connection live in this session.
      2. The exact place_crypto_order / place_equity_order tool schema
         pulled fresh (never guessed), with the order previewed via
         preview_crypto_order / review_equity_order first.
      3. Explicit, informed confirmation from the user for THIS specific
         order — a standing "approve everything" does not satisfy this.
      4. A strategy that has actually backtested as profitable. Neither
         model built so far has.
    None of that has happened yet. Do not remove this guard casually.
    """
    raise NotImplementedError(
        "submit_order() is intentionally not implemented — see docstring. This is a "
        "safety rail, not a bug."
    )
