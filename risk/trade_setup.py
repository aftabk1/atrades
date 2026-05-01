"""
Trade setup: entry price, stop loss, partial take-profit, trailing stop, and position size.

Stop loss = MAX(MIN(entry - 2×ATR14, SwingLow×0.995), entry × 0.80)
  Takes the wider of ATR and 10-day support, floored at 80% of entry (max 20% stop).

Exit plan:
  - Partial exit (PARTIAL_EXIT_PCT of shares) at 2R, capped at 1.5× entry price
  - Trail remainder with 2×ATR trailing stop or previous candle low (whichever is tighter)

Position size = (portfolio × MAX_PORTFOLIO_RISK) / risk_per_share,  (MAX_PORTFOLIO_RISK = 1%)
clipped so the resulting allocation never exceeds MAX_POSITION_SIZE of portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from risk.position_sizer import PositionSizer
from strategy.breakout_signals import BreakoutSignals


@dataclass
class TradeSetup:
    symbol: str
    score: float
    entry_price: float
    stop_loss: float
    target_price: float     # partial exit price (2R)
    trail_atr: float        # trailing stop distance for second half (2×ATR)
    shares: int             # total shares
    partial_shares: int     # shares to exit at target_price
    trail_shares: int       # shares to trail after partial exit
    dollar_risk: float
    dollar_reward: float
    risk_reward: float
    portfolio_pct: float


def calculate_setup(
    signals: BreakoutSignals,
    score: float,
    portfolio_value: float,
) -> TradeSetup | None:
    """
    Build a TradeSetup from a BreakoutSignals bundle.
    Returns None when ATR is zero or risk-per-share is non-positive.
    """
    entry = signals.current_price
    atr   = signals.atr_14

    if atr <= 0 or entry <= 0 or portfolio_value <= 0:
        return None

    # ── Stop loss ──────────────────────────────────────────────────────────
    atr_stop     = entry - config.BREAKOUT_ATR_STOP_MULT * atr   # 2×ATR below entry
    support_stop = signals.support_level * 0.995                  # 0.5% cushion below 10-day swing low
    stop_floor   = entry * (1.0 - config.BREAKOUT_MAX_STOP_PCT)  # never below 80% of entry

    # Take the wider of ATR and support, then enforce the floor
    stop = max(min(atr_stop, support_stop), stop_floor)
    if stop >= entry:
        return None

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return None

    # ── Partial target (2R, capped at 1.5× entry) ────────────────────────
    target = min(entry + config.PARTIAL_EXIT_R * risk_per_share, entry * 1.5)

    # ── Trailing stop distance ─────────────────────────────────────────────
    trail_atr = config.TRAIL_ATR_MULT * atr

    # ── Position sizing ───────────────────────────────────────────────────
    dollar_risk_budget = portfolio_value * config.MAX_PORTFOLIO_RISK
    shares_by_risk     = int(dollar_risk_budget / risk_per_share)

    sizer          = PositionSizer(config.MAX_POSITION_SIZE)
    shares_by_size = int(sizer.shares_for_budget(sizer.budget(portfolio_value), entry))

    shares = max(min(shares_by_risk, shares_by_size), 1)

    # ── Split into partial + trail ─────────────────────────────────────────
    partial_shares = max(int(shares * config.PARTIAL_EXIT_PCT), 1) if shares >= 2 else shares
    trail_shares   = shares - partial_shares

    dollar_risk   = shares * risk_per_share
    dollar_reward = partial_shares * (target - entry)   # reward from partial exit only
    rr_achieved   = (target - entry) / risk_per_share
    portfolio_pct = (shares * entry) / portfolio_value * 100

    return TradeSetup(
        symbol=signals.symbol,
        score=score,
        entry_price=round(entry, 2),
        stop_loss=round(stop, 2),
        target_price=round(target, 2),
        trail_atr=round(trail_atr, 2),
        shares=shares,
        partial_shares=partial_shares,
        trail_shares=trail_shares,
        dollar_risk=round(dollar_risk, 2),
        dollar_reward=round(dollar_reward, 2),
        risk_reward=round(rr_achieved, 2),
        portfolio_pct=round(portfolio_pct, 1),
    )
