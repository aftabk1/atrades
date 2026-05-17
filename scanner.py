"""
A1TRADES Breakout Scanner
────────────────────────
Identifies high-probability breakout setups in US equities using a
multi-factor scoring model augmented with:
  • Institutional accumulation signals (OBV, CMF, up/down vol, block days)
  • Bull-trap / false-breakout detection (5 warning signs with penalty)
  • Market regime filter (BULL/SIDEWAYS/BEAR/HIGH_VOL — gates the scanner)
  • Parameter optimizer (grid search over backtest results)

Usage:
  python scanner.py                             # scan S&P 500, table output
  python scanner.py --symbols AAPL NVDA AMD     # scan specific symbols
  python scanner.py --execute                   # scan + Alpaca bracket orders
  python scanner.py --intraday                  # run every 5 min during market hours
  python scanner.py --intraday --interval 1     # every 1 minute
  python scanner.py --backtest                  # 1-year backtest on 50 symbols
  python scanner.py --optimize                  # grid-search parameter optimisation
  python scanner.py --optimize --symbols AAPL MSFT NVDA --days 252
  python scanner.py --json                      # also print JSON
  python scanner.py --no-regime                 # bypass regime filter (for testing)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta

# Ensure Unicode output works on Windows consoles (cp1252 -> UTF-8)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import schedule
from loguru import logger

try:
    from tabulate import tabulate as _tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False

import config
from broker.alpaca_client import AlpacaClient
from data.universe import StockUniverse
from data.market_data import MarketDataClient
from execution.bracket_orders import BracketOrderExecutor
from risk.trade_setup import TradeSetup, calculate_setup
from strategy.breakout_signals import detect_all
from strategy.breakout_scorer import BreakoutScorer
from strategy.market_regime import detect_regime, MarketRegime, Regime

_TOP_N = 5


# ── Scanner class ─────────────────────────────────────────────────────────────

class BreakoutScanner:
    def __init__(self, execute: bool = False, regime_aware: bool = True) -> None:
        self._universe     = StockUniverse()
        self._market       = MarketDataClient()
        self._scorer       = BreakoutScorer()
        self._broker        = AlpacaClient()
        self._executor      = BracketOrderExecutor(self._broker) if execute else None
        self._execute       = execute
        self._regime_aware  = regime_aware and config.REGIME_AWARE_SCANNING
        self._last_regime:           MarketRegime | None = None
        self._last_breadth:          float = 0.5   # fraction of stocks above 20MA, updated each scan
        self._last_top_unqualified:  list[dict] = []  # top scorers below threshold (radar)

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self, symbols: list[str] | None = None,
             min_score_override: float | None = None,
             force: bool = False) -> list[dict]:
        """
        Full breakout scan with regime gate, accumulation scoring, and trap filtering.
        Returns up to _TOP_N candidates sorted by descending score.
        """
        scan_symbols = symbols or self._universe.get_symbols()
        logger.info(f"Scanning {len(scan_symbols)} symbols...")

        if not force:
            try:
                if not self._broker.is_market_open():
                    logger.warning("Market is currently closed — skipping scan")
                    return []
            except Exception as exc:
                logger.warning(f"Could not determine market status ({exc}) — proceeding anyway")

        market_data = self._market.get_daily_bars(scan_symbols, days=252)
        spy_data    = self._market.get_spy_data(days=252)

        # ── Market regime detection ───────────────────────────────────────────
        regime = detect_regime(spy_data)
        self._last_regime = regime

        base_min = min_score_override if min_score_override is not None else config.BREAKOUT_MIN_SCORE
        effective_min_score = (
            max(base_min, regime.min_score_override)
            if self._regime_aware else base_min
        )

        if self._regime_aware and not regime.scan_recommended:
            logger.warning(
                f"Regime={regime.state.value} — scanning with elevated score floor "
                f"({effective_min_score:.0f}). Consider pausing new positions."
            )

        # ── Market breadth: % of scanned stocks above 20-day MA ──────────────
        breadth_above = sum(
            1 for df in market_data.values()
            if df is not None and len(df) >= 22
            and float(df["close"].iloc[-1]) > float(df["close"].iloc[-22:-1].mean())
        )
        breadth_total  = sum(1 for df in market_data.values() if df is not None and len(df) >= 22)
        breadth_pct    = breadth_above / breadth_total if breadth_total > 0 else 0.5
        self._last_breadth = breadth_pct

        portfolio_value = self._broker.get_portfolio_value()
        open_positions  = {p.symbol for p in self._broker.get_all_positions()}
        logger.info(
            f"Portfolio: ${portfolio_value:,.2f} | "
            f"Open: {', '.join(open_positions) or 'none'} | "
            f"Regime: {regime.state.value} | "
            f"Breadth: {breadth_above}/{breadth_total} ({breadth_pct:.0%}) above 20MA"
        )

        candidates: list[dict] = []
        all_scored: list[tuple] = []  # (score, symbol, signals) — all symbols that passed base filters

        for symbol, df in market_data.items():
            if symbol in open_positions:
                continue

            earnings_date = self._market.get_earnings_date(symbol)
            signals = detect_all(symbol, df, spy_data, earnings_date)

            if signals is None:
                continue

            # Score includes per-symbol signals + market breadth component
            score = self._scorer.score(signals, breadth_pct)

            # Track every scored symbol for the radar (before threshold filter)
            all_scored.append((score, symbol, signals))

            if score < effective_min_score:
                continue

            setup = calculate_setup(signals, score, portfolio_value)
            if setup is None:
                continue

            candidates.append(_build_candidate(signals, setup, self._scorer, regime, breadth_pct))

        candidates.sort(key=lambda c: c["score"], reverse=True)
        top = candidates[:_TOP_N]

        # Build top-5 radar: highest-scoring symbols that did NOT qualify
        qualified_syms = {c["symbol"] for c in candidates}
        all_scored.sort(key=lambda t: t[0], reverse=True)
        top_unqualified: list[dict] = []
        for score, symbol, signals in all_scored:
            if symbol in qualified_syms:
                continue
            top_unqualified.append({
                "symbol":       symbol,
                "score":        round(score, 1),
                "current_price": round(signals.current_price, 2),
                "entry":        round(signals.current_price, 2),
                "volume_ratio": round(signals.volume_surge.value, 2),
                "rsi":          round(signals.rsi_zone.value, 1),
                "rs_vs_spy":    round(signals.relative_strength.value, 2),
                "is_trap":      signals.bull_trap.is_trap if signals.bull_trap else False,
                "regime":       regime.state.value,
                "gap_pct":      round(signals.gap_pct * 100, 2),
            })
            if len(top_unqualified) >= 5:
                break
        self._last_top_unqualified = top_unqualified

        if self._execute and top:
            placed = self._place_orders(top, open_positions)
            if placed:
                try:
                    from data.store import save_trade, update_trade_breakout_level
                    # Build a symbol→breakout_level map from candidates
                    bl_map = {c["symbol"]: c.get("breakout_level", 0) for c in top}
                    for order in placed:
                        save_trade(order)
                        sym = order.get("symbol", "")
                        if sym in bl_map and bl_map[sym]:
                            update_trade_breakout_level(order.get("buy_order_id", ""), bl_map[sym])
                except Exception:
                    pass  # store errors must never crash the scanner

        logger.info(
            f"Scan complete — {len(candidates)} qualified, "
            f"returning top {len(top)} (regime={regime.state.value})"
        )
        return top

    # ── Output ────────────────────────────────────────────────────────────────

    def print_table(self, candidates: list[dict]) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── Regime header ─────────────────────────────────────────────────────
        if self._last_regime:
            r = self._last_regime
            regime_labels = {
                Regime.BULL_TREND:      "BULL TREND",
                Regime.SIDEWAYS:        "SIDEWAYS",
                Regime.BEAR_TREND:      "BEAR TREND",
                Regime.HIGH_VOLATILITY: "HIGH VOLATILITY",
            }
            rlabel = regime_labels.get(r.state, r.state.value)
            print(f"\n{'-' * 82}")
            print(
                f"  MARKET REGIME: {rlabel:18s} | "
                f"ADX={r.adx:.1f} | SPY {'^^' if r.spy_above_200ma else 'vv'} 200MA | "
                f"Slope={r.spy_slope_20d:+.1f}% | Vol={r.realized_vol_20d:.1f}% | "
                f"Breadth={self._last_breadth:.0%} above 20MA"
            )
            if not r.scan_recommended:
                print("  [!] Regime not favourable -- new positions carry elevated risk")

        print(f"{'-' * 82}")
        print(f"  BREAKOUT SCANNER  .  {ts}  .  top {_TOP_N} candidates")
        print(f"{'-' * 82}")

        if not candidates:
            print("  No breakout candidates found.\n")
            return

        if _HAS_TABULATE:
            rows = [
                [
                    f"#{i + 1}",
                    c["symbol"],
                    f"{c['score']:.0f}/100",
                    f"${c['entry']:.2f}",
                    f"${c['stop']:.2f}",
                    f"${c['target']:.2f}",
                    f"{c['risk_reward']:.1f}x",
                    f"{c['volume_ratio']:.1f}x",
                    f"{c['rsi']:.0f}",
                    f"{c['rs_vs_spy']:+.1f}%",
                    f"{c['accum_score']:.0%}",
                    "TRAP" if c["is_trap"] else "ok",
                    f"{c['shares']} sh",
                    f"${c['dollar_risk']:,.0f}",
                ]
                for i, c in enumerate(candidates)
            ]
            headers = [
                "#", "Symbol", "Score", "Entry", "Stop", "Target",
                "R:R", "Vol", "RSI", "RS/SPY", "Accum", "Trap", "Qty", "$Risk",
            ]
            print(_tabulate(rows, headers=headers, tablefmt="simple"))
        else:
            for i, c in enumerate(candidates, 1):
                trap_tag = " [TRAP?]" if c["is_trap"] else ""
                print(
                    f"  #{i} {c['symbol']:6s}  score={c['score']:.0f}  "
                    f"entry=${c['entry']:.2f}  stop=${c['stop']:.2f}  "
                    f"target=${c['target']:.2f}  R:R={c['risk_reward']:.1f}x  "
                    f"accum={c['accum_score']:.0%}{trap_tag}"
                )

        print()
        for c in candidates:
            _print_signal_detail(c)

    def to_json(self, candidates: list[dict]) -> str:
        regime_data = {}
        if self._last_regime:
            r = self._last_regime
            regime_data = {
                "state": r.state.value,
                "adx": r.adx,
                "spy_above_200ma": r.spy_above_200ma,
                "slope_20d": r.spy_slope_20d,
                "realized_vol_20d": r.realized_vol_20d,
                "score_multiplier": r.score_multiplier,
                "scan_recommended": r.scan_recommended,
            }

        payload = {
            "timestamp": datetime.now().isoformat(),
            "scanner_version": "2.0",
            "market_regime": regime_data,
            "count": len(candidates),
            "candidates": [
                {
                    "rank": i + 1,
                    "symbol": c["symbol"],
                    "score": c["score"],
                    "is_trap_warning": c["is_trap"],
                    "accumulation_score": c["accum_score"],
                    "trade_setup": {
                        "entry_price":    c["entry"],
                        "stop_loss":      c["stop"],
                        "partial_target": c["target"],
                        "trail_atr":      c["trail_atr"],
                        "risk_reward":    c["risk_reward"],
                        "shares":         c["shares"],
                        "partial_shares": c["partial_shares"],
                        "trail_shares":   c["trail_shares"],
                        "dollar_risk":    c["dollar_risk"],
                        "dollar_reward":  c["dollar_reward"],
                        "portfolio_pct":  c["portfolio_pct"],
                    },
                    "metrics": {
                        "volume_ratio":       c["volume_ratio"],
                        "rsi":                c["rsi"],
                        "rs_vs_spy_pct":      c["rs_vs_spy"],
                        "breakout_20d":       c["breakout_20d"],
                        "high_52w_pct":       c["high_52w_pct"],
                        "vcp_contractions":   c["vcp_contractions"],
                        "market_breadth_pct": c["market_breadth_pct"],
                    },
                    "score_breakdown": c["score_breakdown"],
                    "signal_detail": c["signals"],
                    "trap_warnings": c["trap_warnings"],
                    "accumulation_detail": c["accum_detail"],
                }
                for i, c in enumerate(candidates)
            ],
        }
        return json.dumps(payload, indent=2)

    # ── Intraday loop ─────────────────────────────────────────────────────────

    def run_intraday(self, interval_minutes: int = 5) -> None:
        logger.info(f"Intraday mode: scanning every {interval_minutes} minute(s)")

        def _cycle() -> None:
            if not self._broker.is_market_open():
                logger.debug("Market closed — intraday scan skipped")
                return
            candidates = self.scan()
            self.print_table(candidates)

        schedule.every(interval_minutes).minutes.do(_cycle)
        _cycle()

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Intraday scanner stopped")

    # ── Private ───────────────────────────────────────────────────────────────

    def _place_orders(self, candidates: list[dict], open_positions: set[str]) -> list[dict]:
        mode_label = "PAPER" if config.IS_PAPER else "*** REAL MONEY ***"
        logger.info(f"Placing orders [{mode_label}] — {len(candidates)} candidate(s), {len(open_positions)} already open")
        slots   = config.MAX_CONCURRENT_TRADES - len(open_positions)
        placed  = []
        for c in candidates[:max(slots, 0)]:
            if c["is_trap"]:
                logger.warning(f"Skipping {c['symbol']} — flagged as potential bull trap")
                continue
            result = self._executor.place(_dict_to_trade_setup(c))
            if result:
                logger.info(f"Order placed: {result}")
                placed.append(result)
        return placed


# ── Backtest runner ───────────────────────────────────────────────────────────

def run_backtest(
    symbols: list[str] | None = None,
    days: int = 365,
    max_symbols: int = 50,
) -> None:
    from backtest.engine import BacktestEngine

    universe = StockUniverse()
    market   = MarketDataClient()

    scan_symbols = (symbols or universe.get_symbols())[:max_symbols]
    logger.info(f"Fetching {days}d of data for {len(scan_symbols)} symbols...")

    market_data = market.get_daily_bars(scan_symbols, days=days)
    spy_data    = market.get_spy_data(days=days)
    market_data.pop("SPY", None)

    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    engine     = BacktestEngine(initial_capital=config.BACKTEST_INITIAL_CAPITAL)
    results    = engine.run(
        market_data=market_data,
        spy_data=spy_data,
        start_date=start_date,
        max_concurrent=config.MAX_CONCURRENT_TRADES,
    )
    engine.print_report(results)


# ── Parameter optimiser ───────────────────────────────────────────────────────

def run_optimize(
    symbols: list[str] | None = None,
    days: int = 365,
    max_symbols: int = 30,
    metric: str = "sharpe",
) -> None:
    from backtest.optimizer import ParameterOptimizer

    universe = StockUniverse()
    market   = MarketDataClient()

    scan_symbols = (symbols or universe.get_symbols())[:max_symbols]
    logger.info(
        f"Optimizer: fetching {days}d of data for {len(scan_symbols)} symbols..."
    )

    market_data = market.get_daily_bars(scan_symbols, days=days)
    spy_data    = market.get_spy_data(days=days)
    market_data.pop("SPY", None)

    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    opt = ParameterOptimizer(
        market_data=market_data,
        spy_data=spy_data,
        initial_capital=config.BACKTEST_INITIAL_CAPITAL,
        start_date=start_date,
    )
    results = opt.run(metric=metric)
    opt.print_report(results)
    saved = opt.save_best_params(results)
    print(f"  Best params written to: {saved}")
    print(
        f"  To apply, merge {saved} into your .env file "
        f"and restart the scanner.\n"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_candidate(signals, setup: TradeSetup, scorer: BreakoutScorer,
                     regime: MarketRegime, breadth_pct: float = 0.5) -> dict:
    accum   = signals.accumulation
    trap    = signals.bull_trap

    accum_detail = {}
    if accum is not None:
        accum_detail = {
            "obv_trend":     accum.obv_trend.description,
            "chaikin_mf":    accum.chaikin_mf.description,
            "up_down_vol":   accum.up_down_vol_ratio.description,
            "inst_days":     accum.institutional_days.description,
        }

    trap_warnings = trap.warnings if trap else []

    return {
        "symbol":       signals.symbol,
        "score":        setup.score,
        "entry":        setup.entry_price,
        "stop":           setup.stop_loss,
        "target":         setup.target_price,
        "trail_atr":      setup.trail_atr,
        "shares":         setup.shares,
        "partial_shares": setup.partial_shares,
        "trail_shares":   setup.trail_shares,
        "dollar_risk":    setup.dollar_risk,
        "dollar_reward":  setup.dollar_reward,
        "risk_reward":    setup.risk_reward,
        "portfolio_pct":  setup.portfolio_pct,
        "gap_pct":            round(signals.gap_pct * 100, 2),
        "volume_ratio":       round(signals.volume_surge.value, 2),
        "rsi":                round(signals.rsi_zone.value, 1),
        "rs_vs_spy":          round(signals.relative_strength.value, 2),
        "breakout_level":     round(signals.breakout_level, 2),
        "breakout_20d":       signals.breakout_20d.triggered,
        "breakout_10d":       signals.breakout_10d.triggered,
        "high_52w_pct":       round(signals.high_52w_proximity.value, 2),
        "vcp_contractions":   int(signals.vcp.value),
        "market_breadth_pct": round(breadth_pct * 100, 1),
        "accum_score":        accum.composite_score if accum else 0.0,
        "is_trap":            trap.is_trap if trap else False,
        "trap_score":         trap.trap_score if trap else 0.0,
        "trap_warnings":      trap_warnings,
        "accum_detail":       accum_detail,
        "regime":             regime.state.value,
        "score_breakdown":    scorer.breakdown(signals, breadth_pct),
        "signals": {
            "breakout_20d":        signals.breakout_20d.description,
            "breakout_10d":        signals.breakout_10d.description,
            "consolidation":       signals.consolidation.description,
            "higher_lows":         signals.higher_lows.description,
            "vcp":                 signals.vcp.description,
            "high_52w_proximity":  signals.high_52w_proximity.description,
            "volume_surge":        signals.volume_surge.description,
            "rsi":                 signals.rsi_zone.description,
            "relative_strength":   signals.relative_strength.description,
            "earnings":            signals.earnings_proximity.description,
        },
    }


def _print_signal_detail(c: dict) -> None:
    trap_tag = "  [!] TRAP WARNING" if c["is_trap"] else ""
    print(
        f"  -- {c['symbol']}  "
        f"(score {c['score']:.0f}/100 | accum {c['accum_score']:.0%}{trap_tag}) --"
    )

    # Base signals
    bd = c.get("score_breakdown", {})
    for name, desc in c["signals"].items():
        if not desc:
            continue
        pts    = bd.get(name, 0.0)
        marker = "[+]" if pts > 0 else "[-]"
        print(f"    {marker} [{pts:5.1f}pt] {desc}")

    # Accumulation detail
    if c.get("accum_detail"):
        bonus = bd.get("accum_bonus", 0.0)
        print(f"    -- Accumulation (bonus {bonus:+.1f}pt) --")
        for k, desc in c["accum_detail"].items():
            if desc:
                print(f"       * {desc}")

    # Trap warnings
    if c["trap_warnings"]:
        penalty = bd.get("trap_penalty", 0.0)
        print(f"    -- Bull Trap Warnings (penalty {penalty:.1f}pt) --")
        for w in c["trap_warnings"]:
            print(f"       [!] {w}")

    print(
        f"    -> Entry ${c['entry']:.2f}  |  Stop ${c['stop']:.2f}  "
        f"|  Partial ({c['partial_shares']} sh) ${c['target']:.2f}  "
        f"|  Trail ({c['trail_shares']} sh) -${c['trail_atr']:.2f}  "
        f"|  R:R {c['risk_reward']:.1f}x  |  Risk ${c['dollar_risk']:,.0f}\n"
    )


def _dict_to_trade_setup(c: dict) -> TradeSetup:
    return TradeSetup(
        symbol=c["symbol"],
        score=c["score"],
        entry_price=c["entry"],
        stop_loss=c["stop"],
        target_price=c["target"],
        trail_atr=c["trail_atr"],
        shares=c["shares"],
        partial_shares=c["partial_shares"],
        trail_shares=c["trail_shares"],
        dollar_risk=c["dollar_risk"],
        dollar_reward=c["dollar_reward"],
        risk_reward=c["risk_reward"],
        portfolio_pct=c["portfolio_pct"],
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="A1TRADES Breakout Scanner v2 — high-probability US equity breakouts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--symbols", nargs="+", metavar="SYM",
                   help="Specific tickers (default: full S&P 500)")
    p.add_argument("--execute", action="store_true",
                   help="Place Alpaca bracket orders (paper or real money depending on IS_PAPER in .env)")
    p.add_argument("--intraday", action="store_true",
                   help="Recurring scan on a schedule")
    p.add_argument("--interval", type=int, default=5, metavar="MINS",
                   help="Intraday interval in minutes (default: 5)")
    p.add_argument("--backtest", action="store_true",
                   help="Run 1-year backtest")
    p.add_argument("--optimize", action="store_true",
                   help="Run parameter grid-search optimisation")
    p.add_argument("--metric", default="sharpe", metavar="M",
                   choices=["sharpe", "win_rate", "profit_factor", "total_return"],
                   help="Optimisation metric (default: sharpe)")
    p.add_argument("--days", type=int, default=365, metavar="N",
                   help="Lookback days for backtest/optimise (default: 365)")
    p.add_argument("--json", action="store_true",
                   help="Also print JSON output")
    p.add_argument("--top", type=int, default=5, metavar="N",
                   help="Candidates to return (default: 5)")
    p.add_argument("--no-regime", action="store_true",
                   help="Bypass market regime filter")
    p.add_argument("--force", action="store_true",
                   help="Run scan even when market is closed")
    p.add_argument("--min-score", type=float, default=None, metavar="N",
                   help="Override BREAKOUT_MIN_SCORE for this run (e.g. 0 to show all candidates)")
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()

    global _TOP_N
    _TOP_N = args.top

    if args.optimize:
        run_optimize(symbols=args.symbols, days=args.days, metric=args.metric)
        return

    if args.backtest:
        run_backtest(symbols=args.symbols, days=args.days)
        return

    scanner = BreakoutScanner(
        execute=args.execute,
        regime_aware=not args.no_regime,
    )

    if args.intraday:
        scanner.run_intraday(interval_minutes=args.interval)
        return

    candidates = scanner.scan(symbols=args.symbols,
                              min_score_override=args.min_score,
                              force=args.force)

    try:
        from data.store import init_db, save_scan
        init_db()
        universe_size = len(scanner._universe.get_symbols())
        save_scan(candidates, universe_size, scanner._last_regime,
                  top_unqualified=scanner._last_top_unqualified)
    except Exception:
        pass

    if args.json:
        print(scanner.to_json(candidates))
    else:
        scanner.print_table(candidates)


if __name__ == "__main__":
    main()
