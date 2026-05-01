"""
Parameter optimizer — vectorized grid search over breakout scanner thresholds.

Architecture (two-phase, fast):
  Phase 1 — PRECOMPUTE (once):
    For every symbol, compute all raw signal metrics (RSI, volume ratio,
    ATR expansion ratio, 20/50-day breakout flag, RS vs SPY) as Pandas
    Series aligned on a common DatetimeIndex.  O(symbols × dates).

  Phase 2 — EVALUATE (per combination):
    Apply parameter thresholds to the precomputed Series — pure boolean
    masking, no DataFrame slicing, no Python loops over rows.
    Simulate trades sequentially only for days that passed the mask.
    O(combinations × signal_days).  Typically 50–200× faster than calling
    detect_all() inside a nested loop.

Expected runtime: ~30–120 seconds for the default 81-combination grid
on 30 symbols with 1 year of history.

Usage (from scanner.py CLI):
  python scanner.py --optimize
  python scanner.py --optimize --symbols AAPL MSFT NVDA --days 252
  python scanner.py --optimize --metric win_rate
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

try:
    from tabulate import tabulate as _tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False

import config
from backtest.engine import Trade, BacktestResults, _try_exit, _build_date_index


# ── Parameter grid ────────────────────────────────────────────────────────────
# 3⁴ = 81 combinations.  Each uses only precomputed arrays — very fast.
DEFAULT_GRID: dict[str, list[Any]] = {
    "BREAKOUT_RSI_LOW":                  [50,   55,   60],
    "BREAKOUT_VOLUME_SURGE_MULT":        [1.3,  1.5,  2.0],
    "BREAKOUT_ATR_EXPANSION_THRESHOLD":  [1.0,  1.2,  1.5],
    "BACKTEST_MAX_HOLD_DAYS":            [10,   15,   20],
}


@dataclass
class OptResult:
    params: dict[str, Any]
    sharpe: float
    win_rate: float
    profit_factor: float
    total_return: float
    max_drawdown: float
    n_trades: int
    elapsed_s: float


# ── Precomputed metric bundle (per symbol) ────────────────────────────────────

@dataclass
class _SymMetrics:
    symbol: str
    df: pd.DataFrame            # full OHLCV

    # Vectorized signal series (index = trading dates)
    breakout_20d: pd.Series     # bool: close > rolling max(20).shift(1)
    breakout_50d: pd.Series     # bool
    vol_ratio:    pd.Series     # float: today_vol / avg_vol_20d (lagged)
    rsi:          pd.Series     # float: RSI(14)
    atr_exp:      pd.Series     # float: ATR(5) / ATR(20)
    rs_vs_spy:    pd.Series     # float: stock 20d return minus SPY 20d return
    base_ok:      pd.Series     # bool: price ≥ 10 AND avg_vol ≥ 1M

    atr14:        pd.Series     # float: ATR(14) — for stop/target calculation


class ParameterOptimizer:
    def __init__(
        self,
        market_data: dict[str, pd.DataFrame],
        spy_data: pd.DataFrame,
        initial_capital: float = config.BACKTEST_INITIAL_CAPITAL,
        start_date: str | None = None,
        end_date: str | None   = None,
        param_grid: dict[str, list] | None = None,
    ) -> None:
        self._market_data     = market_data
        self._spy_data        = spy_data
        self._initial_capital = initial_capital
        self._start_date      = start_date
        self._end_date        = end_date
        self._grid            = param_grid or DEFAULT_GRID
        self._metrics: dict[str, _SymMetrics] = {}

    def run(self, metric: str = "sharpe") -> list[OptResult]:
        """
        Precompute signal metrics once, then evaluate all combinations.
        `metric` selects the ranking key: 'sharpe' | 'win_rate' |
        'profit_factor' | 'total_return'.
        """
        combos = _all_combos(self._grid)

        logger.info("Precomputing signal metrics for all symbols (one-time)...")
        t_pre = time.time()
        self._metrics = self._precompute()
        logger.info(
            f"Precomputation done: {len(self._metrics)} symbols in "
            f"{time.time() - t_pre:.1f}s"
        )

        logger.info(
            f"Optimizer: evaluating {len(combos)} combinations | "
            f"metric={metric}"
        )

        results: list[OptResult] = []
        t_total = time.time()

        for i, params in enumerate(combos, 1):
            t0  = time.time()
            res = self._evaluate(params)
            elapsed = time.time() - t0

            opt = OptResult(
                params=params,
                sharpe=res.sharpe_ratio,
                win_rate=res.win_rate,
                profit_factor=min(res.profit_factor, 99.9),
                total_return=res.total_return_pct,
                max_drawdown=res.max_drawdown_pct,
                n_trades=len(res.closed_trades),
                elapsed_s=elapsed,
            )
            results.append(opt)

            done_pct = i / len(combos) * 100
            elapsed_total = time.time() - t_total
            eta = elapsed_total / i * (len(combos) - i)
            logger.info(
                f"  [{i:3d}/{len(combos)}] {done_pct:5.1f}%  "
                f"sharpe={opt.sharpe:+.2f}  wr={opt.win_rate:.0f}%  "
                f"pf={opt.profit_factor:.2f}  trades={opt.n_trades}  "
                f"({elapsed:.2f}s/combo, ETA {eta:.0f}s)"
            )

        results.sort(key=lambda r: getattr(r, metric), reverse=True)
        logger.info(
            f"Optimization complete in {time.time() - t_total:.1f}s. "
            f"Best {metric}: {getattr(results[0], metric):.3f}"
        )
        return results

    # ── Phase 1: vectorized precomputation ────────────────────────────────────

    def _precompute(self) -> dict[str, _SymMetrics]:
        spy_close = self._spy_data["close"] if not self._spy_data.empty else pd.Series(dtype=float)

        metrics: dict[str, _SymMetrics] = {}
        for symbol, df in self._market_data.items():
            if df is None or len(df) < 60:
                continue
            try:
                metrics[symbol] = _compute_metrics(symbol, df, spy_close)
            except Exception as exc:
                logger.debug(f"Metric precompute failed for {symbol}: {exc}")

        logger.info(f"Precomputed metrics for {len(metrics)} symbols")
        return metrics

    # ── Phase 2: per-combo threshold application + trade simulation ───────────

    def _evaluate(self, params: dict[str, Any]) -> BacktestResults:
        rsi_low     = params["BREAKOUT_RSI_LOW"]
        vol_mult    = params["BREAKOUT_VOLUME_SURGE_MULT"]
        atr_thresh  = params["BREAKOUT_ATR_EXPANSION_THRESHOLD"]
        hold_days   = params["BACKTEST_MAX_HOLD_DAYS"]

        results  = BacktestResults(initial_capital=self._initial_capital)
        capital  = self._initial_capital
        max_conc = config.MAX_CONCURRENT_TRADES

        # Build unified date axis
        all_dates = _build_date_index(
            {s: m.df for s, m in self._metrics.items()},
            self._start_date, self._end_date,
        )
        if len(all_dates) < 5:
            return results

        # Precompute signal masks for every symbol using current thresholds
        signal_masks: dict[str, pd.Series] = {}
        for sym, m in self._metrics.items():
            mask = (
                m.base_ok
                & m.breakout_20d
                & (m.vol_ratio   >= vol_mult)
                & (m.rsi         >= rsi_low)
                & (m.rsi         <= 70.0)
                & (m.atr_exp     >= atr_thresh)
            )
            signal_masks[sym] = mask

        open_trades: list[Trade] = []

        for i, signal_date in enumerate(all_dates[:-1]):
            entry_date = all_dates[i + 1]

            # -- Close out open trades that hit stop/target/timeout --------------
            still_open: list[Trade] = []
            for trade in open_trades:
                sym_df = self._metrics[trade.symbol].df if trade.symbol in self._metrics else None
                result = _try_exit(trade, sym_df, entry_date, i, all_dates)
                if result.outcome == "open":
                    # Manual time-stop using precomputed index
                    entry_idx = next(
                        (j for j, d in enumerate(all_dates) if d == trade.entry_date), None
                    )
                    days_held = (i - entry_idx) if entry_idx is not None else 0
                    if days_held >= hold_days:
                        row = sym_df[sym_df.index == entry_date] if sym_df is not None else pd.DataFrame()
                        if not row.empty:
                            close_px = float(row.iloc[0]["close"])
                            result.exit_date  = entry_date
                            result.exit_price = close_px
                            result.pnl        = (close_px - result.entry_price) * result.shares
                            result.outcome    = "timeout"
                            capital += result.pnl
                        else:
                            still_open.append(result)
                    else:
                        still_open.append(result)
                else:
                    capital += result.pnl
            open_trades = still_open

            # -- Scan for new signals on signal_date ----------------------------
            slots = max_conc - len(open_trades)
            if slots <= 0:
                continue

            held = {t.symbol for t in open_trades}
            new_candidates: list[tuple[float, str]] = []

            for sym, mask in signal_masks.items():
                if sym in held:
                    continue
                if signal_date not in mask.index:
                    continue
                if not bool(mask.loc[signal_date]):
                    continue

                m = self._metrics[sym]
                new_candidates.append((_fast_score(m, signal_date), sym))

            new_candidates.sort(reverse=True)

            for _, sym in new_candidates[:slots]:
                m = self._metrics[sym]
                entry_row = m.df[m.df.index == entry_date]
                if entry_row.empty:
                    continue

                entry_px = float(entry_row.iloc[0]["open"]) * (1 + config.BACKTEST_SLIPPAGE_PCT)
                atr14    = float(m.atr14.loc[signal_date]) if signal_date in m.atr14.index else 0.0

                if atr14 <= 0:
                    continue

                stop   = max(entry_px - config.BREAKOUT_ATR_STOP_MULT * atr14,
                             entry_px * (1 - config.BREAKOUT_MAX_STOP_PCT))
                target = entry_px + config.BREAKOUT_RR_RATIO * (entry_px - stop)
                risk_ps = entry_px - stop

                if risk_ps <= 0:
                    continue

                shares = max(int((capital * config.MAX_PORTFOLIO_RISK) / risk_ps), 1)

                trail_atr      = config.TRAIL_ATR_MULT * atr if atr > 0 else 0.0
                partial_shares = max(int(shares * config.PARTIAL_EXIT_PCT), 1) if shares >= 2 else shares
                trail_shares   = shares - partial_shares
                trade = Trade(
                    symbol=sym,
                    entry_date=entry_date,
                    entry_price=round(entry_px, 4),
                    stop_loss=round(stop, 2),
                    target_price=round(target, 2),
                    trail_atr=round(trail_atr, 2),
                    shares=shares,
                    partial_shares=partial_shares,
                    trail_shares=trail_shares,
                    score=50.0,    # placeholder — full scoring not needed for optim
                    trail_stop=round(stop, 2),
                )
                open_trades.append(trade)
                results.trades.append(trade)

        # Close remaining open trades
        last_date = all_dates[-1]
        for trade in open_trades:
            sym_df = self._metrics[trade.symbol].df if trade.symbol in self._metrics else None
            if sym_df is not None and not sym_df.empty:
                last_close       = float(sym_df.iloc[-1]["close"])
                trade.exit_date  = last_date
                trade.exit_price = last_close
                trade.pnl        = (last_close - trade.entry_price) * trade.shares
                trade.outcome    = "timeout"
                capital         += trade.pnl

        results.final_capital = capital
        return results

    # ── Reporting ─────────────────────────────────────────────────────────────

    def print_report(self, results: list[OptResult], top_n: int = 15) -> None:
        top = results[:top_n]
        print(f"\n{'=' * 74}")
        print("  PARAMETER OPTIMIZATION RESULTS")
        print(f"{'=' * 74}")
        print(f"  Tested {len(results)} combinations | Showing top {len(top)}\n")

        param_keys  = list(self._grid.keys())
        short_keys  = [
            k.replace("BREAKOUT_", "").replace("BACKTEST_", "")[:12]
            for k in param_keys
        ]

        if _HAS_TABULATE:
            rows = [
                [i + 1]
                + [r.params[k] for k in param_keys]
                + [
                    f"{r.sharpe:+.2f}",
                    f"{r.win_rate:.0f}%",
                    f"{r.profit_factor:.2f}",
                    f"{r.total_return:+.1f}%",
                    f"{r.max_drawdown:.1f}%",
                    r.n_trades,
                ]
                for i, r in enumerate(top)
            ]
            headers = (
                ["#"] + short_keys
                + ["Sharpe", "WinRate", "PF", "Return", "MaxDD", "Trades"]
            )
            print(_tabulate(rows, headers=headers, tablefmt="simple"))
        else:
            for i, r in enumerate(top, 1):
                pstr = "  ".join(
                    f"{k.split('_')[-1]}={v}" for k, v in r.params.items()
                )
                print(
                    f"  #{i:2d}  {pstr}  "
                    f"sharpe={r.sharpe:+.2f}  wr={r.win_rate:.0f}%  "
                    f"pf={r.profit_factor:.2f}  trades={r.n_trades}"
                )

        best = results[0]
        print(f"\n  ── Best found (Sharpe {best.sharpe:+.2f}) ──")
        for k, v in best.params.items():
            current = getattr(config, k)
            flag    = "  ← UPDATE RECOMMENDED" if v != current else ""
            print(f"    {k:45s} = {v}  (current: {current}){flag}")
        print()

    def save_best_params(
        self, results: list[OptResult], out_path: str = "best_params.env"
    ) -> Path:
        best = results[0]
        path = Path(out_path)
        lines = [
            "# Optimized breakout scanner parameters",
            f"# Sharpe={best.sharpe:+.2f}  WinRate={best.win_rate:.0f}%  "
            f"PF={best.profit_factor:.2f}  Trades={best.n_trades}",
            "",
        ] + [f"{k}={v}" for k, v in best.params.items()] + [""]
        path.write_text("\n".join(lines))
        logger.info(f"Best params saved → {path}")
        return path


# ── Vectorized metric computation ─────────────────────────────────────────────

def _compute_metrics(
    symbol: str, df: pd.DataFrame, spy_close: pd.Series
) -> _SymMetrics:
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    # 20/50-day high breakout (use prior window — shift(1) ensures no lookahead)
    breakout_20d = close > close.rolling(20, min_periods=20).max().shift(1)
    breakout_50d = close > close.rolling(50, min_periods=50).max().shift(1)

    # Volume ratio (vs lagged 20-day average — lagged to avoid lookahead)
    avg_vol_20 = vol.rolling(20, min_periods=10).mean().shift(1)
    vol_ratio  = vol / avg_vol_20.replace(0, np.nan)

    # RSI(14)
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14, min_periods=7).mean()
    loss  = (-delta.clip(upper=0)).rolling(14, min_periods=7).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # ATR expansion: ATR(5) / ATR(20)
    prev_c = close.shift(1)
    tr     = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    atr5   = tr.rolling(5,  min_periods=3).mean()
    atr20  = tr.rolling(20, min_periods=10).mean()
    atr14  = tr.rolling(14, min_periods=7).mean()
    atr_exp = atr5 / atr20.replace(0, np.nan)

    # RS vs SPY: 20-day return differential
    if spy_close is not None and not spy_close.empty:
        stock_ret = close.pct_change(20)
        spy_ret   = spy_close.reindex(close.index, method="ffill").pct_change(20)
        rs_vs_spy = stock_ret - spy_ret
    else:
        rs_vs_spy = pd.Series(0.0, index=close.index)

    # Base filter: price ≥ $10, lagged 20d avg volume ≥ 1M
    base_ok = (close >= config.BREAKOUT_MIN_PRICE) & (avg_vol_20 >= config.BREAKOUT_MIN_AVG_VOLUME)

    return _SymMetrics(
        symbol=symbol,
        df=df,
        breakout_20d=breakout_20d.fillna(False),
        breakout_50d=breakout_50d.fillna(False),
        vol_ratio=vol_ratio.fillna(0.0),
        rsi=rsi.fillna(50.0),
        atr_exp=atr_exp.fillna(0.0),
        rs_vs_spy=rs_vs_spy.fillna(0.0),
        base_ok=base_ok.fillna(False),
        atr14=atr14.fillna(0.0),
    )


def _fast_score(m: _SymMetrics, signal_date: pd.Timestamp) -> float:
    """Quick 0–100 score from precomputed metrics (no full detect_all needed)."""
    def _get(s: pd.Series) -> float:
        return float(s.loc[signal_date]) if signal_date in s.index else 0.0

    score = 0.0
    score += 20.0                                         # base: breakout confirmed
    score += min(_get(m.vol_ratio) / 3.0 * 25.0, 25.0)  # volume surge (scaled)
    rsi_v  = _get(m.rsi)
    score += 15.0 if 55 <= rsi_v <= 70 else 0.0          # RSI zone
    rs_v   = _get(m.rs_vs_spy)
    score += min(rs_v * 100 / 10.0 * 15.0, 15.0) if rs_v > 0 else 0.0   # RS
    score += 10.0 if bool(m.breakout_50d.loc[signal_date]
                          if signal_date in m.breakout_50d.index else False) else 0.0
    score += 10.0 if _get(m.atr_exp) > 1.2 else 0.0     # ATR expansion
    return min(score, 100.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_combos(grid: dict[str, list]) -> list[dict]:
    keys   = list(grid.keys())
    values = list(grid.values())
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]
