"""
Simple event-driven backtest engine.

Signal fires on day T's close.
Entry is simulated at day T+1's open (plus slippage).
Exit rules evaluated each subsequent day:
  • If today's low  ≤ stop   → exit at stop (loss). Stop takes priority when both hit.
  • If today's high ≥ target → exit at target (win).
  • After BACKTEST_MAX_HOLD_DAYS → exit at close (timeout).
All open trades at the end of the test are closed at the last available price.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

import config
from strategy.breakout_signals import detect_all
from strategy.breakout_scorer import BreakoutScorer
from strategy.sector_rotation import _SYMBOL_TO_ETF as _SECTOR_ETF_MAP
from strategy.market_regime import detect_regime
from risk.trade_setup import calculate_setup

_SCORER = BreakoutScorer()


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_loss: float
    target_price: float     # partial exit at 2R
    trail_atr: float        # trailing stop distance (2×ATR at entry)
    shares: int
    partial_shares: int
    trail_shares: int
    score: float
    # runtime state
    partial_filled: bool = False
    trail_stop: float = 0.0     # tracks current trailing stop level for trail_shares
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    outcome: str = "open"   # "win" | "loss" | "timeout" | "open"


@dataclass
class BacktestResults:
    trades: list[Trade] = field(default_factory=list)
    initial_capital: float = config.BACKTEST_INITIAL_CAPITAL
    final_capital: float = config.BACKTEST_INITIAL_CAPITAL

    # ── Derived metrics ───────────────────────────────────────────────────────

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.outcome != "open"]

    @property
    def win_rate(self) -> float:
        ct = self.closed_trades
        return sum(1 for t in ct if t.outcome == "win") / len(ct) * 100 if ct else 0.0

    @property
    def total_return_pct(self) -> float:
        return (self.final_capital / self.initial_capital - 1) * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.closed_trades if t.pnl > 0)
        gross_loss   = abs(sum(t.pnl for t in self.closed_trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown_pct(self) -> float:
        curve = self._equity_curve()
        if curve.empty or len(curve) < 2:
            return 0.0
        rolling_max = curve.cummax()
        drawdown    = (curve - rolling_max) / rolling_max * 100
        return float(drawdown.min())

    @property
    def sharpe_ratio(self) -> float:
        curve = self._equity_curve()
        if curve.empty or len(curve) < 2:
            return 0.0
        daily_returns = curve.pct_change().dropna()
        if daily_returns.std() == 0:
            return 0.0
        return float(daily_returns.mean() / daily_returns.std() * np.sqrt(252))

    def _equity_curve(self) -> pd.Series:
        pnl_by_date: dict[pd.Timestamp, float] = {}
        for t in self.closed_trades:
            if t.exit_date is not None:
                pnl_by_date[t.exit_date] = pnl_by_date.get(t.exit_date, 0.0) + t.pnl

        if not pnl_by_date:
            return pd.Series(dtype=float)

        running = self.initial_capital
        curve   = {}
        for dt in sorted(pnl_by_date):
            running += pnl_by_date[dt]
            curve[dt] = running

        return pd.Series(curve)


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(self, initial_capital: float = config.BACKTEST_INITIAL_CAPITAL) -> None:
        self.initial_capital = initial_capital

    def run(
        self,
        market_data: dict[str, pd.DataFrame],
        spy_data: pd.DataFrame,
        start_date: Optional[str] = None,
        end_date: Optional[str]   = None,
        max_concurrent: int       = config.MAX_CONCURRENT_TRADES,
        min_score: float          = config.BREAKOUT_MIN_SCORE,
    ) -> BacktestResults:
        """
        Simulate the breakout scanner over historical data.
        `market_data` keys must NOT include "SPY" (passed separately as spy_data).
        """
        results = BacktestResults(initial_capital=self.initial_capital)
        capital = self.initial_capital

        # Pre-fetch EPS growth for all symbols once (avoids per-bar API calls)
        eps_map: dict[str, float | None] = {}
        if config.EARNINGS_FILTER_ENABLED:
            from data.market_data import MarketDataClient as _MDC
            _mdc = _MDC()
            logger.info(f"Fetching EPS growth for {len(market_data)} symbols...")
            eps_map = _mdc.get_eps_growth_bulk(list(market_data.keys()))
            filtered = sum(
                1 for v in eps_map.values()
                if v is not None and (v < 0 or v < config.EARNINGS_MIN_EPS_GROWTH)
            )
            logger.info(f"EPS filter: {filtered}/{len(market_data)} symbols will be excluded")

        # Build a unified date timeline from all symbol DataFrames
        all_dates = _build_date_index(market_data, start_date, end_date)
        if len(all_dates) < 5:
            logger.warning("Insufficient trading days for backtest")
            return results

        logger.info(
            f"Backtest: {len(market_data)} symbols | "
            f"{len(all_dates)} trading days | "
            f"capital=${capital:,.0f}"
        )

        open_trades: list[Trade] = []

        for i, signal_date in enumerate(all_dates[:-1]):
            entry_date = all_dates[i + 1]

            # ── Step 1: Evaluate all open trades on entry_date ────────────────
            still_open: list[Trade] = []
            for trade in open_trades:
                sym_df = market_data.get(trade.symbol)
                # pass previous trading day for prev_low trailing stop
                prev_date = all_dates[i - 1] if i > 0 else None
                result = _try_exit(trade, sym_df, entry_date, i, all_dates, prev_date)
                if result.outcome != "open":
                    capital += result.pnl
                else:
                    still_open.append(result)
            open_trades = still_open

            # ── Step 2: Scan signal_date for new setups ───────────────────────
            slots = max_concurrent - len(open_trades)
            if slots <= 0:
                continue

            # ── Regime gate: skip new entries on bad market days ──────────────
            spy_hist_regime = spy_data[spy_data.index <= signal_date] if not spy_data.empty else pd.DataFrame()
            regime = detect_regime(spy_hist_regime)
            if not regime.scan_recommended:
                continue   # BEAR or HIGH_VOLATILITY — no new entries today

            held_symbols = {t.symbol for t in open_trades}
            candidates: list[tuple[float, object, pd.DataFrame]] = []

            for symbol, full_df in market_data.items():
                if symbol in held_symbols:
                    continue

                # Use only data available at signal_date (no lookahead)
                hist = full_df[full_df.index <= signal_date]
                spy_hist = spy_data[spy_data.index <= signal_date] if not spy_data.empty else pd.DataFrame()

                # Sector exclusion gate
                if config.EXCLUDED_SECTOR_ETFS:
                    etf = _SECTOR_ETF_MAP.get(symbol.upper())
                    if etf and etf in config.EXCLUDED_SECTOR_ETFS:
                        continue

                # EPS growth gate — same logic as scanner Phase 2
                if config.EARNINGS_FILTER_ENABLED:
                    eps = eps_map.get(symbol)
                    if eps is not None and (eps < 0 or eps < config.EARNINGS_MIN_EPS_GROWTH):
                        continue

                signals = detect_all(symbol, hist, spy_hist)
                if signals is None:
                    continue

                score = _SCORER.score(signals)
                if score < min_score:
                    continue

                candidates.append((score, signals, full_df))

            candidates.sort(key=lambda x: x[0], reverse=True)

            for score, signals, full_df in candidates[:slots]:
                entry_row = full_df[full_df.index == entry_date]
                if entry_row.empty:
                    continue

                # Entry at next day's open + slippage
                raw_entry = float(entry_row.iloc[0]["open"])
                entry_px  = raw_entry * (1 + config.BACKTEST_SLIPPAGE_PCT)

                # Recalculate setup at actual entry price
                adj = copy.copy(signals)
                adj.current_price = entry_px

                setup = calculate_setup(adj, score, capital)
                if setup is None or setup.shares < 1:
                    continue

                trade = Trade(
                    symbol=signals.symbol,
                    entry_date=entry_date,
                    entry_price=round(entry_px, 4),
                    stop_loss=setup.stop_loss,
                    target_price=setup.target_price,
                    trail_atr=setup.trail_atr,
                    shares=setup.shares,
                    partial_shares=setup.partial_shares,
                    trail_shares=setup.trail_shares,
                    score=score,
                    trail_stop=setup.stop_loss,  # initialise trail_stop at hard stop
                )
                open_trades.append(trade)
                results.trades.append(trade)

        # ── Close remaining open trades at last available price ───────────────
        last_date = all_dates[-1]
        for trade in open_trades:
            sym_df = market_data.get(trade.symbol)
            if sym_df is not None and not sym_df.empty:
                last_close      = float(sym_df.iloc[-1]["close"])
                trade.exit_date  = last_date
                trade.exit_price = last_close
                trade.pnl        = (last_close - trade.entry_price) * trade.shares
                trade.outcome    = "timeout"
                capital         += trade.pnl

        results.final_capital = capital
        return results

    # ── Report ────────────────────────────────────────────────────────────────

    def print_report(self, results: BacktestResults) -> None:
        ct = results.closed_trades
        wins     = [t for t in ct if t.outcome == "win"]
        losses   = [t for t in ct if t.outcome == "loss"]
        timeouts = [t for t in ct if t.outcome == "timeout"]

        print("\n" + "=" * 58)
        print("  BACKTEST RESULTS")
        print("=" * 58)
        print(f"  Total trades:      {len(ct)}")
        print(f"  Win / Loss / TO:   {len(wins)} / {len(losses)} / {len(timeouts)}")
        print(f"  Win rate:          {results.win_rate:.1f}%")
        print(f"  Profit factor:     {results.profit_factor:.2f}")
        print(f"  Total return:      {results.total_return_pct:+.2f}%")
        print(f"  Max drawdown:      {results.max_drawdown_pct:.2f}%")
        print(f"  Sharpe ratio:      {results.sharpe_ratio:.2f}")
        print(f"  Initial capital:   ${results.initial_capital:>12,.2f}")
        print(f"  Final capital:     ${results.final_capital:>12,.2f}")

        if wins:
            print(f"  Avg win P&L:       ${np.mean([t.pnl for t in wins]):>+,.2f}")
        if losses:
            print(f"  Avg loss P&L:      ${np.mean([t.pnl for t in losses]):>+,.2f}")
        print("=" * 58)

        if ct:
            print("\n  Last 10 closed trades:")
            for t in sorted(ct, key=lambda x: x.exit_date)[-10:]:
                sign = "+" if t.pnl >= 0 else ""
                print(
                    f"    {t.symbol:6s}  "
                    f"{t.entry_date.strftime('%Y-%m-%d')} → {t.exit_date.strftime('%Y-%m-%d')}  "
                    f"P&L: {sign}${t.pnl:>8,.2f}  score={t.score:.0f}  [{t.outcome}]"
                )
        print()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_date_index(
    market_data: dict[str, pd.DataFrame],
    start_date: Optional[str],
    end_date: Optional[str],
) -> list[pd.Timestamp]:
    all_ts: set[pd.Timestamp] = set()
    for df in market_data.values():
        if not df.empty:
            all_ts.update(df.index.tolist())

    dates = sorted(all_ts)
    if start_date:
        cutoff = pd.Timestamp(start_date)
        dates  = [d for d in dates if d >= cutoff]
    if end_date:
        cutoff = pd.Timestamp(end_date)
        dates  = [d for d in dates if d <= cutoff]
    return dates


def _try_exit(
    trade: Trade,
    sym_df: Optional[pd.DataFrame],
    eval_date: pd.Timestamp,
    date_idx: int,
    all_dates: list[pd.Timestamp],
    prev_date: Optional[pd.Timestamp] = None,
) -> Trade:
    """
    Check exit conditions on eval_date. Handles two-phase exit:
      Phase 1 (before partial): stop_loss exits all shares; target_price triggers partial exit.
      Phase 2 (after partial):  trail_stop (MAX of ATR-trail and prev candle low) exits trail_shares.
    Modifies trade in place and returns it.
    """
    if sym_df is None:
        return trade

    day_rows = sym_df[sym_df.index == eval_date]
    if day_rows.empty:
        return trade

    row      = day_rows.iloc[0]
    high     = float(row["high"])
    low      = float(row["low"])
    close_px = float(row["close"])

    entry_idx = next((j for j, d in enumerate(all_dates) if d == trade.entry_date), None)
    days_held = (date_idx - entry_idx) if entry_idx is not None else 0

    if not trade.partial_filled:
        # ── Phase 1: full position, hard stop or partial target ────────────
        if low <= trade.stop_loss:
            trade.exit_date  = eval_date
            trade.exit_price = trade.stop_loss
            trade.pnl        = (trade.stop_loss - trade.entry_price) * trade.shares
            trade.outcome    = "loss"

        elif high >= trade.target_price:
            # Partial exit fills; start trailing the remainder
            partial_pnl         = (trade.target_price - trade.entry_price) * trade.partial_shares
            trade.pnl          += partial_pnl
            trade.partial_filled = True

            if trade.trail_shares < 1:
                # No shares left to trail — trade fully closed
                trade.exit_date  = eval_date
                trade.exit_price = trade.target_price
                trade.outcome    = "win"
            else:
                # Initialise trailing stop: max(hard stop, ATR trail from close, prev candle low)
                atr_trail_stop = close_px - trade.trail_atr
                prev_low = _get_prev_low(sym_df, prev_date)
                trade.trail_stop = max(trade.stop_loss, atr_trail_stop, prev_low)

        elif days_held >= config.BACKTEST_MAX_HOLD_DAYS:
            trade.exit_date  = eval_date
            trade.exit_price = close_px
            trade.pnl        = (close_px - trade.entry_price) * trade.shares
            trade.outcome    = "timeout"

    else:
        # ── Phase 2: trailing the remainder ───────────────────────────────
        # Ratchet trail_stop up using ATR trail from today's close and prev candle low
        atr_trail_stop   = close_px - trade.trail_atr
        prev_low         = _get_prev_low(sym_df, prev_date)
        trade.trail_stop = max(trade.trail_stop, atr_trail_stop, prev_low)

        if low <= trade.trail_stop:
            trail_pnl        = (trade.trail_stop - trade.entry_price) * trade.trail_shares
            trade.pnl       += trail_pnl
            trade.exit_date  = eval_date
            trade.exit_price = trade.trail_stop
            trade.outcome    = "win" if trade.pnl > 0 else "loss"

        elif days_held >= config.BACKTEST_MAX_HOLD_DAYS:
            trail_pnl        = (close_px - trade.entry_price) * trade.trail_shares
            trade.pnl       += trail_pnl
            trade.exit_date  = eval_date
            trade.exit_price = close_px
            trade.outcome    = "timeout"

    return trade


def _get_prev_low(sym_df: pd.DataFrame, prev_date: Optional[pd.Timestamp]) -> float:
    """Return the low of prev_date bar, or 0.0 if unavailable."""
    if prev_date is None:
        return 0.0
    rows = sym_df[sym_df.index == prev_date]
    if rows.empty:
        return 0.0
    return float(rows.iloc[0]["low"])
