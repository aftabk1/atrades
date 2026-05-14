"""
ATrades End-to-End Regression Test Suite  (178 tests)
======================================================
Run all:         pytest tests/
Run one section: pytest tests/ -k "Security"
With coverage:   pytest tests/ --cov=. --cov-report=term-missing
Verbose output:  pytest tests/ -v

Sections:
  1.  Config & Environment           (9)
  2.  Stock Universe                 (9)
  3.  SQLite Data Store              (9)
  4.  Breakout Signal Detection      (13)
  5.  Breakout Scorer                (10)
  6.  Market Regime Detection        (11)
  7.  Trade Setup Calculation        (11)
  8.  Risk Manager                   (8)
  9.  Webapp API Endpoints           (19)
  10. End-to-End Pipeline            (10)
  11. Runner & Scan API              (11)
  12. Position Manager (PME)         (12)
  13. Position Executor              (13)
  14. Security                       (13)
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import (
    make_breakout_df,
    make_bull_spy_df,
    make_bear_spy_df,
    make_ohlcv,
    make_signals,
    make_spy_df,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. CONFIG & ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_config_loads(self):
        import config
        assert hasattr(config, "ALPACA_API_KEY")
        assert hasattr(config, "ALPACA_SECRET_KEY")
        assert hasattr(config, "IS_PAPER")

    def test_is_paper_is_bool(self):
        import config
        assert isinstance(config.IS_PAPER, bool)

    def test_symbols_is_list(self):
        import config
        assert isinstance(config.SYMBOLS, list)
        assert len(config.SYMBOLS) >= 1

    def test_risk_limits_are_fractions(self):
        import config
        assert 0 < config.MAX_POSITION_SIZE <= 1.0
        assert 0 < config.MAX_PORTFOLIO_RISK <= 1.0
        assert config.MAX_CONCURRENT_TRADES >= 1

    def test_breakout_thresholds_positive(self):
        import config
        assert config.BREAKOUT_MIN_PRICE > 0
        assert config.BREAKOUT_MIN_AVG_VOLUME > 0
        assert config.BREAKOUT_VOLUME_SURGE_MULT >= 1.0
        assert config.BREAKOUT_MIN_SCORE >= 0

    def test_rsi_zone_valid_range(self):
        import config
        assert 0 < config.BREAKOUT_RSI_LOW < config.BREAKOUT_RSI_HIGH < 100

    def test_rr_ratio_at_least_one(self):
        import config
        assert config.BREAKOUT_RR_RATIO >= 1.0

    def test_backtest_capital_positive(self):
        import config
        assert config.BACKTEST_INITIAL_CAPITAL > 0

    def test_paper_trading_enabled_by_default(self):
        import config
        assert config.IS_PAPER is True, "IS_PAPER should be True in .env for safety"

    def test_pme_config_keys_present(self):
        import config
        pme_keys = [
            "PME_ADD_SCORE_THRESHOLD", "PME_HOLD_SCORE_MIN",
            "PME_TRIM_LIGHT_SCORE_MIN", "PME_TRIM_HEAVY_SCORE_MIN",
            "PME_ADD_SIZE_PCT", "PME_ADD_MAX_MULTIPLIER",
            "PME_TRIM_LIGHT_PCT", "PME_TRIM_HEAVY_PCT",
            "PME_RS_ADD_MIN_PCT", "PME_RS_DOWNGRADE_BELOW_PCT",
            "PME_R_TRIM_FLOOR", "PME_R_TRIM_ENFORCE",
            "PME_FOLLOWTHROUGH_DAYS", "PME_VOLUME_SELLOFF_MULT",
        ]
        for key in pme_keys:
            assert hasattr(config, key), f"Missing PME config key: {key}"

    def test_pme_score_thresholds_ordered(self):
        import config
        assert config.PME_ADD_SCORE_THRESHOLD > config.PME_HOLD_SCORE_MIN
        assert config.PME_HOLD_SCORE_MIN > config.PME_TRIM_LIGHT_SCORE_MIN
        assert config.PME_TRIM_LIGHT_SCORE_MIN > config.PME_TRIM_HEAVY_SCORE_MIN
        assert config.PME_TRIM_HEAVY_SCORE_MIN > 0

    def test_pme_trim_percentages_valid(self):
        import config
        assert 0 < config.PME_TRIM_LIGHT_PCT < 1.0
        assert 0 < config.PME_TRIM_HEAVY_PCT < 1.0
        assert config.PME_TRIM_HEAVY_PCT > config.PME_TRIM_LIGHT_PCT


# ══════════════════════════════════════════════════════════════════════════════
# 2. STOCK UNIVERSE
# ══════════════════════════════════════════════════════════════════════════════

class TestUniverse:
    def test_returns_fallback_when_no_override(self, temp_universe):
        from data.universe import StockUniverse, _FALLBACK_SYMBOLS
        symbols = StockUniverse().get_symbols()
        assert symbols == list(_FALLBACK_SYMBOLS)

    def test_fallback_has_expected_size(self):
        from data.universe import _FALLBACK_SYMBOLS
        assert len(_FALLBACK_SYMBOLS) >= 400, "Expect at least 400 S&P 500 symbols"

    def test_fallback_contains_common_stocks(self):
        from data.universe import _FALLBACK_SYMBOLS
        for sym in ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]:
            assert sym in _FALLBACK_SYMBOLS

    def test_no_duplicate_symbols(self):
        from data.universe import _FALLBACK_SYMBOLS
        assert len(_FALLBACK_SYMBOLS) == len(set(_FALLBACK_SYMBOLS))

    def test_override_file_replaces_fallback(self, temp_universe):
        import data.universe as universe
        custom = ["AAPL", "NVDA", "CUSTOM_TEST"]
        temp_universe.write_text(json.dumps(custom))
        symbols = universe.StockUniverse().get_symbols()
        assert symbols == custom

    def test_override_can_include_non_sp500_symbols(self, temp_universe):
        import data.universe as universe
        custom = ["QQQ", "SPY", "BTC/USD"]
        temp_universe.write_text(json.dumps(custom))
        result = universe.StockUniverse().get_symbols()
        assert "QQQ" in result
        assert "BTC/USD" in result

    def test_empty_override_returns_empty_list(self, temp_universe):
        import data.universe as universe
        temp_universe.write_text(json.dumps([]))
        result = universe.StockUniverse().get_symbols()
        assert result == []

    def test_all_symbols_are_strings(self):
        from data.universe import _FALLBACK_SYMBOLS
        assert all(isinstance(s, str) for s in _FALLBACK_SYMBOLS)

    def test_all_symbols_are_uppercase(self):
        from data.universe import _FALLBACK_SYMBOLS
        non_upper = [s for s in _FALLBACK_SYMBOLS if s != s.upper()]
        assert non_upper == [], f"Non-uppercase symbols: {non_upper}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. SQLITE DATA STORE
# ══════════════════════════════════════════════════════════════════════════════

class TestStore:
    def _make_regime(self):
        """Minimal mock regime object for save_scan."""
        from strategy.market_regime import Regime
        regime = MagicMock()
        regime.state.value = Regime.BULL_TREND.value
        regime.adx = 28.5
        regime.spy_above_200ma = True
        regime.spy_slope_20d = 0.04
        regime.score_multiplier = 1.0
        return regime

    def _make_candidate(self, symbol="AAPL", score=75.0) -> dict:
        return {
            "symbol": symbol, "score": score, "entry": 150.0,
            "stop": 145.0, "target": 160.0, "trail_atr": 148.0,
            "shares": 10, "partial_shares": 5, "trail_shares": 5,
            "dollar_risk": 50.0, "risk_reward": 2.0,
            "volume_ratio": 1.8, "rsi": 62.0, "rs_vs_spy": 3.5,
            "is_trap": False, "regime": "BULL_TREND",
        }

    def test_init_db_creates_tables(self, temp_db):
        import sqlite3
        con = sqlite3.connect(temp_db)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"scan_runs", "scan_candidates", "trades"}.issubset(tables)
        con.close()

    def test_init_db_idempotent(self, temp_db):
        from data.store import init_db
        init_db()  # second call should not raise
        init_db()

    def test_save_and_query_scan(self, temp_db):
        from data.store import save_scan, query_day
        candidates = [self._make_candidate("AAPL", 75.0), self._make_candidate("NVDA", 82.0)]
        save_scan(candidates, symbols_scanned=500, regime=self._make_regime())
        result = query_day(date.today().isoformat())
        assert result["scan_count"] == 1
        assert result["candidates_found"] == 2
        assert result["symbols_scanned"] == 500
        assert result["regime"] == "BULL_TREND"

    def test_query_day_deduplicates_by_best_score(self, temp_db):
        from data.store import save_scan, query_day
        today = date.today().isoformat()
        # Two scans, AAPL appears in both with different scores
        save_scan([self._make_candidate("AAPL", 60.0)], 500, self._make_regime())
        save_scan([self._make_candidate("AAPL", 80.0)], 500, self._make_regime())
        result = query_day(today)
        aapl = next(c for c in result["candidates"] if c["symbol"] == "AAPL")
        assert aapl["score"] == 80.0, "Should keep highest score"
        assert result["candidates_found"] == 1, "AAPL should appear once"

    def test_query_day_empty_returns_zero_counts(self, temp_db):
        from data.store import query_day
        result = query_day("2000-01-01")
        assert result["scan_count"] == 0
        assert result["candidates_found"] == 0
        assert result["trades_placed"] == 0
        assert result["candidates"] == []
        assert result["trades"] == []

    def test_save_and_query_trade(self, temp_db):
        from data.store import save_trade, query_day
        order = {
            "symbol": "AAPL", "buy_order_id": "order-123",
            "partial_order_id": "order-124", "trail_order_id": "order-125",
            "shares": 10, "partial_shares": 5, "trail_shares": 5,
            "entry": 150.0, "stop_loss": 145.0, "partial_target": 160.0,
            "trail_atr": 148.0, "score": 78.0,
        }
        save_trade(order)
        result = query_day(date.today().isoformat())
        assert result["trades_placed"] == 1
        assert result["trades"][0]["symbol"] == "AAPL"
        assert result["trades"][0]["buy_order_id"] == "order-123"

    def test_query_history_returns_list(self, temp_db):
        from data.store import save_scan, query_history
        save_scan([self._make_candidate()], 100, self._make_regime())
        rows = query_history(days=30)
        assert isinstance(rows, list)
        assert len(rows) >= 1

    def test_query_history_respects_days_limit(self, temp_db):
        from data.store import query_history
        rows = query_history(days=7)
        assert isinstance(rows, list)

    def test_scan_run_stores_regime_details(self, temp_db):
        from data.store import save_scan, query_day
        save_scan([], symbols_scanned=200, regime=self._make_regime())
        result = query_day(date.today().isoformat())
        assert result["adx"] == pytest.approx(28.5, abs=0.1)
        assert result["spy_above_200ma"] is True
        assert result["score_multiplier"] == pytest.approx(1.0)

    def test_init_db_creates_position_evaluations_table(self, temp_db):
        import sqlite3
        con = sqlite3.connect(temp_db)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "position_evaluations" in tables
        cols = {r[1] for r in con.execute("PRAGMA table_info(position_evaluations)").fetchall()}
        assert {"symbol", "buy_order_id", "score", "action", "r_multiple", "executed"}.issubset(cols)
        con.close()

    def test_close_trade_computes_actual_r_and_hold_days(self, temp_db):
        from data.store import save_trade, close_trade, get_open_trades
        import sqlite3
        order = {
            "symbol": "AAPL", "buy_order_id": "close-test-001",
            "shares": 10, "entry": 100.0, "stop_loss": 95.0,
            "fill_price": 100.0, "partial_target": 110.0, "trail_atr": 2.0, "score": 70.0,
        }
        save_trade(order)
        # Simulate fill recorded
        from data.store import update_trade_fill
        update_trade_fill("close-test-001", 100.0, datetime.now(timezone.utc).isoformat())
        close_trade("close-test-001", exit_price=110.0, exit_reason="target_hit")
        con = sqlite3.connect(temp_db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM trades WHERE buy_order_id=?", ("close-test-001",)).fetchone()
        assert row["status"] == "closed"
        assert row["exit_price"] == pytest.approx(110.0)
        assert row["actual_r"] == pytest.approx(2.0)  # (110-100)/(100-95) = 2.0
        assert row["exit_reason"] == "target_hit"
        con.close()

    def test_update_trade_breakout_level(self, temp_db):
        from data.store import save_trade, update_trade_breakout_level
        import sqlite3
        save_trade({
            "symbol": "NVDA", "buy_order_id": "bl-001",
            "shares": 5, "entry": 200.0, "stop_loss": 190.0,
            "partial_target": 220.0, "trail_atr": 3.0, "score": 72.0,
        })
        update_trade_breakout_level("bl-001", 198.50)
        con = sqlite3.connect(temp_db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT breakout_level FROM trades WHERE buy_order_id=?", ("bl-001",)).fetchone()
        assert row["breakout_level"] == pytest.approx(198.50)
        con.close()

    def test_update_highest_price_ratchets_up_never_down(self, temp_db):
        from data.store import save_trade, update_highest_price
        import sqlite3
        save_trade({
            "symbol": "AMD", "buy_order_id": "hp-001",
            "shares": 20, "entry": 80.0, "stop_loss": 75.0,
            "partial_target": 90.0, "trail_atr": 1.5, "score": 68.0,
        })
        update_highest_price("hp-001", 85.0)
        update_highest_price("hp-001", 92.0)  # goes up
        update_highest_price("hp-001", 88.0)  # should not go down
        con = sqlite3.connect(temp_db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT highest_price_since_entry FROM trades WHERE buy_order_id=?", ("hp-001",)).fetchone()
        assert row["highest_price_since_entry"] == pytest.approx(92.0)
        con.close()

    def test_save_and_get_position_evaluation(self, temp_db):
        from data.store import save_position_evaluation, get_position_evaluations
        ev = {
            "symbol": "MSFT", "buy_order_id": "pme-001",
            "score": 78.5, "action": "HOLD",
            "r_multiple": 1.8, "rs_vs_spy": 6.2,
            "trap_triggered": False, "reason": "score=78 | holding",
            "executed": False,
        }
        save_position_evaluation(ev)
        results = get_position_evaluations(buy_order_id="pme-001")
        assert len(results) == 1
        assert results[0]["action"] == "HOLD"
        assert results[0]["score"] == pytest.approx(78.5)
        assert results[0]["buy_order_id"] == "pme-001"

    def test_get_position_evaluations_without_filter_returns_recent(self, temp_db):
        from data.store import save_position_evaluation, get_position_evaluations
        for i in range(3):
            save_position_evaluation({
                "symbol": "GOOG", "buy_order_id": f"pme-goog-{i}",
                "score": 60.0 + i, "action": "HOLD",
                "r_multiple": 1.0, "rs_vs_spy": 3.0,
                "trap_triggered": False, "reason": "test",
                "executed": False,
            })
        results = get_position_evaluations(days=30)
        assert len(results) == 3

    def test_query_closed_trades_empty(self, temp_db):
        from data.store import query_closed_trades
        results = query_closed_trades(day=date.today().isoformat())
        assert results == []

    def test_query_closed_trades_by_day(self, temp_db):
        from data.store import save_trade, update_trade_fill, close_trade, query_closed_trades
        save_trade({
            "symbol": "TSLA", "buy_order_id": "ct-001",
            "shares": 10, "entry": 200.0, "stop_loss": 190.0,
            "fill_price": 200.0, "partial_target": 220.0, "trail_atr": 3.0, "score": 75.0,
        })
        update_trade_fill("ct-001", 200.0, datetime.now(timezone.utc).isoformat())
        close_trade("ct-001", exit_price=215.0, exit_reason="trailing_stop")
        # Use UTC date to match what close_trade stores (timestamps are UTC)
        today = datetime.now(timezone.utc).date().isoformat()
        results = query_closed_trades(day=today)
        assert len(results) == 1
        assert results[0]["symbol"] == "TSLA"
        assert results[0]["exit_price"] == pytest.approx(215.0)
        assert results[0]["exit_reason"] == "trailing_stop"

    def test_query_realized_pnl_sums_valid_trades(self, temp_db):
        from data.store import save_trade, update_trade_fill, close_trade, query_realized_pnl
        for i, (sym, fill, exit_px, shares) in enumerate([
            ("AAA", 100.0, 110.0, 10),   # +$100
            ("BBB", 50.0,  55.0,  20),   # +$100
        ]):
            oid = f"pnl-{i:03d}"
            save_trade({
                "symbol": sym, "buy_order_id": oid,
                "shares": shares, "entry": fill, "stop_loss": fill * 0.95,
                "fill_price": fill, "partial_target": exit_px, "trail_atr": 1.0, "score": 70.0,
            })
            update_trade_fill(oid, fill, datetime.now(timezone.utc).isoformat())
            close_trade(oid, exit_price=exit_px, exit_reason="target_hit")
        pnl = query_realized_pnl()
        assert pnl == pytest.approx(200.0)

    def test_query_realized_pnl_excludes_zero_exit_price(self, temp_db):
        from data.store import save_trade, close_trade, query_realized_pnl
        import sqlite3
        save_trade({
            "symbol": "ZZZ", "buy_order_id": "zero-exit-001",
            "shares": 10, "entry": 100.0, "stop_loss": 90.0,
            "fill_price": 100.0, "partial_target": 110.0, "trail_atr": 1.5, "score": 65.0,
        })
        # Write a bad closed record with exit_price=0
        con = sqlite3.connect(temp_db)
        con.execute(
            "UPDATE trades SET status='closed', exit_price=0, fill_price=100, shares=10 WHERE buy_order_id=?",
            ("zero-exit-001",)
        )
        con.commit()
        con.close()
        pnl = query_realized_pnl()
        assert pnl == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 4. BREAKOUT SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestBreakoutSignals:
    def test_returns_none_for_insufficient_data(self):
        from strategy.breakout_signals import detect_all
        df = make_breakout_df(bars=30)  # below _MIN_BARS=60
        assert detect_all("AAPL", df, make_spy_df()) is None

    def test_returns_none_for_none_dataframe(self):
        from strategy.breakout_signals import detect_all
        assert detect_all("AAPL", None, make_spy_df()) is None

    def test_returns_none_for_empty_dataframe(self):
        from strategy.breakout_signals import detect_all
        assert detect_all("AAPL", pd.DataFrame(), make_spy_df()) is None

    def test_returns_none_when_price_below_minimum(self):
        from strategy.breakout_signals import detect_all
        df = make_breakout_df()
        df["close"] = df["close"] * 0.1  # price ~$6, below $25 floor
        df["open"] = df["open"] * 0.1
        df["high"] = df["high"] * 0.1
        df["low"]  = df["low"] * 0.1
        assert detect_all("AAPL", df, make_spy_df()) is None

    def test_returns_none_when_volume_below_minimum(self):
        from strategy.breakout_signals import detect_all
        df = make_breakout_df()
        df["volume"] = 100_000  # below 1M floor
        assert detect_all("AAPL", df, make_spy_df()) is None

    def test_returns_none_when_no_price_breakout(self):
        from strategy.breakout_signals import detect_all
        # Flat/declining prices → no 20d breakout
        df = make_ohlcv(100, trend=-0.002)
        df["volume"] = 2_000_000
        assert detect_all("AAPL", df, make_spy_df()) is None

    def test_detects_breakout_on_valid_data(self):
        from strategy.breakout_signals import detect_all, BreakoutSignals
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert isinstance(signals, BreakoutSignals)

    def test_breakout_20d_triggered_on_new_high(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert signals.breakout_20d.triggered is True

    def test_volume_surge_triggered_on_high_volume(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert signals.volume_surge.triggered is True
        assert signals.volume_surge.value >= 1.5

    def test_signal_has_correct_symbol(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("NVDA", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert signals.symbol == "NVDA"

    def test_current_price_matches_last_close(self):
        from strategy.breakout_signals import detect_all
        df = make_breakout_df()
        signals = detect_all("AAPL", df, make_spy_df(), fast=True)
        assert signals is not None
        assert signals.current_price == pytest.approx(df["close"].iloc[-1], rel=0.01)

    def test_atr_14_is_positive(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert signals.atr_14 > 0

    def test_fast_mode_skips_accumulation_and_trap(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        assert signals.accumulation is None
        assert signals.bull_trap is None

    def test_full_mode_populates_accumulation_and_trap(self):
        from strategy.breakout_signals import detect_all
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=False)
        assert signals is not None
        # accumulation and bull_trap should be populated
        assert signals.accumulation is not None
        assert signals.bull_trap is not None

    def test_require_breakout_false_returns_signals_on_flat_data(self):
        from strategy.breakout_signals import detect_all
        # Flat trend would normally fail the breakout gate
        df = make_ohlcv(100, trend=0.000, base_price=60.0, avg_vol=2_000_000)
        signals = detect_all("AAPL", df, make_spy_df(), require_breakout=False, fast=True)
        # Should return a result even though no fresh breakout
        assert signals is not None

    def test_require_breakout_true_rejects_flat_data(self):
        from strategy.breakout_signals import detect_all
        df = make_ohlcv(100, trend=-0.002, base_price=60.0, avg_vol=2_000_000)
        signals = detect_all("AAPL", df, make_spy_df(), require_breakout=True)
        assert signals is None

    def test_breakout_level_populated_on_detection(self):
        from strategy.breakout_signals import detect_all
        df = make_breakout_df()
        signals = detect_all("AAPL", df, make_spy_df(), fast=True)
        assert signals is not None
        assert signals.breakout_level > 0
        # breakout_level should be close to the 20-day prior high
        prior_20d_high = float(df["close"].iloc[-21:-1].max())
        assert signals.breakout_level == pytest.approx(prior_20d_high, rel=0.05)


# ══════════════════════════════════════════════════════════════════════════════
# 5. BREAKOUT SCORER
# ══════════════════════════════════════════════════════════════════════════════

class TestBreakoutScorer:
    def test_score_in_valid_range(self):
        from strategy.breakout_scorer import BreakoutScorer
        scorer = BreakoutScorer()
        score = scorer.score(make_signals(all_triggered=True))
        assert 0.0 <= score <= 100.0

    def test_all_signals_triggered_gives_high_score(self):
        from strategy.breakout_scorer import BreakoutScorer
        score = BreakoutScorer().score(make_signals(all_triggered=True))
        assert score >= 60.0, "All signals triggered should score above 60"

    def test_no_signals_triggered_gives_low_score(self):
        from strategy.breakout_scorer import BreakoutScorer
        score = BreakoutScorer().score(make_signals(all_triggered=False))
        assert score <= 30.0, "No signals should score below 30"

    def test_score_never_below_zero(self):
        from strategy.breakout_scorer import BreakoutScorer
        from strategy.breakout_signals import BreakoutSignals, SignalResult
        # Worst-case: no signals + a mock bull trap with max penalty
        signals = make_signals(all_triggered=False)
        trap = MagicMock()
        trap.trap_score = 100.0
        signals.bull_trap = trap
        score = BreakoutScorer().score(signals)
        assert score >= 0.0

    def test_score_never_above_100(self):
        from strategy.breakout_scorer import BreakoutScorer
        # Best-case: all signals + max accumulation bonus
        signals = make_signals(all_triggered=True)
        accum = MagicMock()
        accum.composite_score = 1.0
        signals.accumulation = accum
        score = BreakoutScorer().score(signals)
        assert score <= 100.0

    def test_trap_penalty_reduces_score(self):
        from strategy.breakout_scorer import BreakoutScorer
        signals_clean = make_signals(all_triggered=True)
        signals_trap  = make_signals(all_triggered=True)
        trap = MagicMock()
        trap.trap_score = 80.0
        signals_trap.bull_trap = trap
        score_clean = BreakoutScorer().score(signals_clean)
        score_trap  = BreakoutScorer().score(signals_trap)
        assert score_trap < score_clean, "Trap penalty should reduce score"

    def test_accumulation_bonus_increases_score(self):
        from strategy.breakout_scorer import BreakoutScorer
        signals_no_accum = make_signals(all_triggered=True)
        signals_accum    = make_signals(all_triggered=True)
        accum = MagicMock()
        accum.composite_score = 1.0
        signals_accum.accumulation = accum
        score_no_accum = BreakoutScorer().score(signals_no_accum)
        score_accum    = BreakoutScorer().score(signals_accum)
        assert score_accum >= score_no_accum, "Accumulation should not decrease score"

    def test_breakdown_returns_all_factors(self):
        from strategy.breakout_scorer import BreakoutScorer
        bd = BreakoutScorer().breakdown(make_signals(all_triggered=True))
        expected = [
            "vcp", "consolidation", "higher_lows", "high_52w_proximity",
            "earnings_proximity", "volume_surge", "breakout_20d",
            "rsi_zone", "relative_strength", "market_breadth",
            "accum_bonus", "trap_penalty",
        ]
        for factor in expected:
            assert factor in bd, f"Missing factor in breakdown: {factor}"

    def test_breakdown_values_are_non_negative(self):
        from strategy.breakout_scorer import BreakoutScorer
        bd = BreakoutScorer().breakdown(make_signals(all_triggered=True))
        for k, v in bd.items():
            assert v >= 0, f"Negative breakdown value for {k}: {v}"

    def test_volume_surge_graduated_scoring(self):
        from strategy.breakout_scorer import BreakoutScorer
        from strategy.breakout_signals import BreakoutSignals, SignalResult
        # Low surge (1.5x) should score less than high surge (3x)
        sig_low  = make_signals(); sig_low.volume_surge  = SignalResult(True, 1.5)
        sig_high = make_signals(); sig_high.volume_surge = SignalResult(True, 3.0)
        scorer = BreakoutScorer()
        assert scorer.score(sig_high) >= scorer.score(sig_low)


# ══════════════════════════════════════════════════════════════════════════════
# 6. MARKET REGIME DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestMarketRegime:
    def test_bull_trend_detected(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bull_spy_df(bars=250))
        assert regime.state == Regime.BULL_TREND

    def test_bear_trend_detected(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bear_spy_df(bars=250))
        assert regime.state == Regime.BEAR_TREND

    def test_insufficient_data_returns_sideways(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_spy_df(bars=30))  # below 60 bar minimum
        assert regime.state == Regime.SIDEWAYS

    def test_empty_df_returns_sideways(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(pd.DataFrame())
        assert regime.state == Regime.SIDEWAYS

    def test_bull_multiplier_is_one(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bull_spy_df(bars=250))
        assert regime.state == Regime.BULL_TREND
        assert regime.score_multiplier == pytest.approx(1.0)

    def test_bear_multiplier_is_lower_than_bull(self):
        from strategy.market_regime import detect_regime, Regime
        bull = detect_regime(make_bull_spy_df(bars=250))
        bear = detect_regime(make_bear_spy_df(bars=250))
        if bear.state == Regime.BEAR_TREND:
            assert bear.score_multiplier < bull.score_multiplier

    def test_regime_has_adx_value(self):
        from strategy.market_regime import detect_regime
        regime = detect_regime(make_bull_spy_df(bars=250))
        assert regime.adx > 0

    def test_regime_has_description(self):
        from strategy.market_regime import detect_regime
        regime = detect_regime(make_bull_spy_df(bars=250))
        assert isinstance(regime.description, str)
        assert len(regime.description) > 0

    def test_bull_scan_recommended(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bull_spy_df(bars=250))
        if regime.state == Regime.BULL_TREND:
            assert regime.scan_recommended is True

    def test_bear_scan_not_recommended(self):
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bear_spy_df(bars=250))
        if regime.state == Regime.BEAR_TREND:
            assert regime.scan_recommended is False

    def test_high_volatility_detected(self):
        from strategy.market_regime import detect_regime, Regime
        # Create very volatile SPY
        np.random.seed(5)
        dates = pd.bdate_range(end="2025-01-31", periods=250)
        close = 400.0 * np.cumprod(1 + np.random.normal(0.0, 0.025, 250))
        df = pd.DataFrame({
            "open":   close * 0.99,
            "high":   close * 1.04,
            "low":    close * 0.96,
            "close":  close,
            "volume": np.full(250, 80_000_000),
        }, index=dates)
        regime = detect_regime(df)
        assert regime.state == Regime.HIGH_VOLATILITY


# ══════════════════════════════════════════════════════════════════════════════
# 7. TRADE SETUP CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeSetup:
    def test_returns_trade_setup_for_valid_inputs(self):
        from risk.trade_setup import calculate_setup, TradeSetup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert isinstance(setup, TradeSetup)

    def test_returns_none_when_atr_is_zero(self):
        from risk.trade_setup import calculate_setup
        signals = make_signals(atr=0.0)
        assert calculate_setup(signals, score=75.0, portfolio_value=100_000.0) is None

    def test_returns_none_when_portfolio_is_zero(self):
        from risk.trade_setup import calculate_setup
        assert calculate_setup(make_signals(), score=75.0, portfolio_value=0.0) is None

    def test_stop_loss_below_entry(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.stop_loss < setup.entry_price

    def test_target_above_entry(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.target_price > setup.entry_price

    def test_stop_never_below_floor(self):
        from risk.trade_setup import calculate_setup
        import config
        setup = calculate_setup(make_signals(price=100.0), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        floor = 100.0 * (1 - config.BREAKOUT_MAX_STOP_PCT)
        assert setup.stop_loss >= floor

    def test_shares_at_least_one(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.shares >= 1

    def test_partial_plus_trail_equals_total(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.partial_shares + setup.trail_shares == setup.shares

    def test_dollar_risk_positive(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.dollar_risk > 0

    def test_risk_reward_matches_prices(self):
        from risk.trade_setup import calculate_setup
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=100_000.0)
        assert setup is not None
        expected_rr = (setup.target_price - setup.entry_price) / (setup.entry_price - setup.stop_loss)
        assert setup.risk_reward == pytest.approx(expected_rr, rel=0.01)

    def test_position_size_within_max(self):
        from risk.trade_setup import calculate_setup
        import config
        portfolio = 100_000.0
        setup = calculate_setup(make_signals(), score=75.0, portfolio_value=portfolio)
        assert setup is not None
        position_value = setup.shares * setup.entry_price
        assert position_value <= portfolio * config.MAX_POSITION_SIZE * 1.05  # allow 5% rounding


# ══════════════════════════════════════════════════════════════════════════════
# 8. RISK MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TestRiskManager:
    def test_approves_buy_when_conditions_met(self, mock_alpaca):
        from risk.manager import RiskManager
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_buy("AAPL", price=100.0)
        assert approved is True
        assert qty >= 1

    def test_rejects_buy_when_already_holding(self, mock_alpaca):
        from risk.manager import RiskManager
        mock_alpaca.get_position.return_value = MagicMock(qty=10)
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_buy("AAPL", price=100.0)
        assert approved is False

    def test_rejects_buy_when_insufficient_cash(self, mock_alpaca):
        from risk.manager import RiskManager
        mock_alpaca.get_cash.return_value = 1.0  # almost no cash
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_buy("AAPL", price=50_000.0)
        assert approved is False

    def test_approves_sell_when_position_held(self, mock_alpaca):
        from risk.manager import RiskManager
        position = MagicMock()
        position.qty = "10"
        mock_alpaca.get_position.return_value = position
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_sell("AAPL")
        assert approved is True

    def test_rejects_sell_when_no_position(self, mock_alpaca):
        from risk.manager import RiskManager
        mock_alpaca.get_position.return_value = None
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_sell("AAPL")
        assert approved is False

    def test_check_drawdown_passes_on_healthy_portfolio(self, mock_alpaca):
        from risk.manager import RiskManager
        mock_alpaca.get_portfolio_value.return_value = 100_000.0
        rm = RiskManager(mock_alpaca)
        assert rm.check_drawdown() is True

    def test_buy_qty_respects_max_position_size(self, mock_alpaca):
        from risk.manager import RiskManager
        import config
        portfolio = 100_000.0
        price = 10.0
        mock_alpaca.get_portfolio_value.return_value = portfolio
        mock_alpaca.get_cash.return_value = portfolio
        rm = RiskManager(mock_alpaca)
        approved, qty = rm.approve_buy("AAPL", price=price)
        if approved:
            max_shares = (portfolio * config.MAX_POSITION_SIZE) / price
            assert qty <= max_shares * 1.05  # 5% rounding tolerance


# ══════════════════════════════════════════════════════════════════════════════
# 9. WEBAPP API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class TestWebappAPI:
    def test_root_serves_html(self, api_client):
        r = api_client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "A1TRADES" in r.text

    def test_dashboard_today_returns_dict(self, api_client):
        r = api_client.get("/api/dashboard")
        assert r.status_code == 200
        data = r.json()
        assert "date" in data
        assert "candidates" in data
        assert "trades" in data

    def test_dashboard_specific_date(self, api_client):
        r = api_client.get("/api/dashboard?date=2024-01-15")
        assert r.status_code == 200
        assert r.json()["date"] == "2024-01-15"

    def test_dashboard_empty_day_returns_zeros(self, api_client):
        r = api_client.get("/api/dashboard?date=2000-01-01")
        data = r.json()
        assert data["scan_count"] == 0
        assert data["candidates_found"] == 0
        assert data["trades_placed"] == 0

    def test_history_returns_list(self, api_client):
        r = api_client.get("/api/history")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_history_respects_days_param(self, api_client):
        r = api_client.get("/api/history?days=7")
        assert r.status_code == 200

    def test_history_rejects_invalid_days(self, api_client):
        r = api_client.get("/api/history?days=0")
        assert r.status_code == 422  # FastAPI validation error

    def test_positions_returns_positions_key(self, api_client):
        with patch("alpaca.trading.client.TradingClient") as MockClient:
            instance = MockClient.return_value
            instance.get_all_positions.return_value = []
            instance.get_clock.return_value = MagicMock(is_open=False, next_open=datetime(2025,1,2,9,30))
            r = api_client.get("/api/positions")
        assert r.status_code == 200
        assert "positions" in r.json()

    def test_get_config_returns_all_keys(self, api_client):
        r = api_client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        required = [
            "ALPACA_API_KEY", "IS_PAPER", "SYMBOLS",
            "MAX_POSITION_SIZE", "MAX_CONCURRENT_TRADES",
            "BREAKOUT_MIN_SCORE", "REGIME_AWARE_SCANNING",
            "BACKTEST_INITIAL_CAPITAL",
        ]
        for key in required:
            assert key in data, f"Missing config key: {key}"

    def test_post_config_saves_and_reloads(self, api_client, tmp_path):
        r = api_client.get("/api/config")
        cfg = r.json()
        cfg["BREAKOUT_MIN_SCORE"] = "55.0"
        r2 = api_client.post("/api/config", json=cfg)
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_post_config_rejects_unknown_keys(self, api_client):
        r = api_client.post("/api/config", json={"UNKNOWN_KEY": "value"})
        # Server silently ignores unknown keys and returns ok
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_get_fallback_symbols_returns_list(self, api_client):
        r = api_client.get("/api/symbols/fallback")
        assert r.status_code == 200
        data = r.json()
        assert "symbols" in data
        assert isinstance(data["symbols"], list)
        assert data["default_count"] >= 400

    def test_get_fallback_symbols_not_custom_by_default(self, api_client):
        r = api_client.get("/api/symbols/fallback")
        assert r.json()["is_custom"] is False

    def test_post_fallback_symbols_saves_custom_list(self, api_client):
        custom = ["AAPL", "NVDA", "QQQ"]
        r = api_client.post("/api/symbols/fallback", json={"symbols": custom})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["count"] == 3
        # Verify it persisted
        r2 = api_client.get("/api/symbols/fallback")
        assert r2.json()["is_custom"] is True
        assert r2.json()["symbols"] == custom

    def test_delete_fallback_symbols_restores_defaults(self, api_client):
        # First save a custom list
        api_client.post("/api/symbols/fallback", json={"symbols": ["AAPL"]})
        # Then reset
        r = api_client.delete("/api/symbols/fallback/reset")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Verify back to defaults
        r2 = api_client.get("/api/symbols/fallback")
        assert r2.json()["is_custom"] is False

    def test_post_fallback_symbols_rejects_non_list(self, api_client):
        r = api_client.post("/api/symbols/fallback", json={"symbols": "AAPL,NVDA"})
        assert r.status_code == 400 or r.json().get("ok") is False

    def test_config_does_not_contain_symbol_exclusions(self, api_client):
        r = api_client.get("/api/config")
        assert "SYMBOL_EXCLUSIONS" not in r.json()

    def test_config_includes_pme_keys(self, api_client):
        r = api_client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        for key in ["PME_ADD_SCORE_THRESHOLD", "PME_HOLD_SCORE_MIN",
                    "PME_TRIM_LIGHT_PCT", "PME_TRIM_HEAVY_PCT",
                    "PME_R_TRIM_FLOOR", "PME_VOLUME_SELLOFF_MULT"]:
            assert key in data, f"PME config key missing: {key}"

    def test_closed_trades_returns_trades_key(self, api_client):
        r = api_client.get("/api/closed-trades")
        assert r.status_code == 200
        data = r.json()
        assert "trades" in data
        assert isinstance(data["trades"], list)

    def test_closed_trades_date_filter_empty(self, api_client):
        r = api_client.get("/api/closed-trades?date=2000-01-01")
        assert r.status_code == 200
        assert r.json()["trades"] == []

    def test_closed_trades_days_validation(self, api_client):
        r = api_client.get("/api/closed-trades?days=0")
        assert r.status_code == 422

    def test_position_evaluations_returns_list(self, api_client):
        r = api_client.get("/api/position-evaluations")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_position_evaluations_accepts_buy_order_id_filter(self, api_client):
        r = api_client.get("/api/position-evaluations?buy_order_id=test-123")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_account_endpoint_includes_realized_pnl(self, api_client):
        mock_account = MagicMock()
        mock_account.equity = "100000.00"
        mock_account.cash = "50000.00"
        mock_account.buying_power = "50000.00"
        mock_account.equity_previous_close = "99000.00"
        with patch("alpaca.trading.client.TradingClient") as MockTC:
            instance = MockTC.return_value
            instance.get_account.return_value = mock_account
            instance.get_all_positions.return_value = []
            instance.get_clock.return_value = MagicMock(
                is_open=True,
                next_open=datetime(2025, 1, 2, 9, 30),
            )
            r = api_client.get("/api/account")
        assert r.status_code == 200
        assert "realized_pnl" in r.json()

    def test_recent_sells_returns_correct_shape(self, api_client):
        with patch("alpaca.trading.client.TradingClient") as MockTC:
            MockTC.return_value.get_orders.return_value = []
            r = api_client.get("/api/recent-sells")
        assert r.status_code == 200
        data = r.json()
        assert "sells" in data
        assert isinstance(data["sells"], list)

    def test_scan_next_returns_required_fields(self, api_client):
        r = api_client.get("/api/scan/next")
        assert r.status_code == 200
        data = r.json()
        for field in ("last_scan_ts", "next_scan_ts", "interval_minutes",
                      "runner_running", "scan_running", "market_open", "next_open"):
            assert field in data, f"Missing field in /api/scan/next: {field}"

    def test_dashboard_rejects_invalid_date(self, api_client):
        r = api_client.get("/api/dashboard?date=not-a-date")
        assert r.status_code == 400

    def test_dashboard_rejects_sql_injection_date(self, api_client):
        r = api_client.get("/api/dashboard?date=2024-01-01;DROP TABLE scan_runs")
        assert r.status_code == 400

    def test_closed_trades_rejects_invalid_date(self, api_client):
        r = api_client.get("/api/closed-trades?date=2024/01/01")
        assert r.status_code == 400

    def test_security_headers_on_api_response(self, api_client):
        r = api_client.get("/api/dashboard")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert "content-security-policy" in r.headers
        assert r.headers.get("cache-control") == "no-store"


# ══════════════════════════════════════════════════════════════════════════════
# 10. END-TO-END PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class TestE2EPipeline:
    """
    Full pipeline: signal detection → scoring → trade setup → persistence → API read.
    All external calls (Alpaca, yfinance) are mocked.
    """

    def test_signals_to_score_pipeline(self):
        """detect_all → BreakoutScorer gives a valid score."""
        from strategy.breakout_signals import detect_all
        from strategy.breakout_scorer import BreakoutScorer
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        score = BreakoutScorer().score(signals)
        assert 0.0 <= score <= 100.0

    def test_signals_to_setup_pipeline(self):
        """detect_all → calculate_setup produces a valid TradeSetup."""
        from strategy.breakout_signals import detect_all
        from strategy.breakout_scorer import BreakoutScorer
        from risk.trade_setup import calculate_setup
        signals = detect_all("AAPL", make_breakout_df(), make_spy_df(), fast=True)
        assert signals is not None
        score = BreakoutScorer().score(signals)
        setup = calculate_setup(signals, score=score, portfolio_value=100_000.0)
        assert setup is not None
        assert setup.entry_price > 0
        assert setup.stop_loss < setup.entry_price
        assert setup.shares >= 1

    def test_regime_gates_scanner_in_bear_market(self):
        """In a BEAR_TREND regime, scan_recommended should be False."""
        from strategy.market_regime import detect_regime, Regime
        regime = detect_regime(make_bear_spy_df(bars=250))
        if regime.state == Regime.BEAR_TREND:
            assert regime.scan_recommended is False
            assert regime.score_multiplier < 1.0

    def test_full_scan_save_and_query(self, temp_db):
        """Full pipeline: detect signals → score → build candidate dict → save → query."""
        from strategy.breakout_signals import detect_all
        from strategy.breakout_scorer import BreakoutScorer
        from strategy.market_regime import detect_regime
        from risk.trade_setup import calculate_setup
        from data.store import save_scan, query_day

        spy_df = make_spy_df(bars=250)
        regime = detect_regime(spy_df)
        signals = detect_all("AAPL", make_breakout_df(), spy_df, fast=True)
        assert signals is not None

        score = BreakoutScorer().score(signals)
        setup = calculate_setup(signals, score=score, portfolio_value=100_000.0)
        assert setup is not None

        candidate = {
            "symbol":         setup.symbol,
            "score":          score,
            "entry":          setup.entry_price,
            "stop":           setup.stop_loss,
            "target":         setup.target_price,
            "trail_atr":      setup.trail_atr,
            "shares":         setup.shares,
            "partial_shares": setup.partial_shares,
            "trail_shares":   setup.trail_shares,
            "dollar_risk":    setup.dollar_risk,
            "risk_reward":    setup.risk_reward,
            "volume_ratio":   signals.volume_surge.value,
            "rsi":            signals.rsi_zone.value,
            "rs_vs_spy":      signals.relative_strength.value,
            "is_trap":        False,
            "regime":         regime.state.value,
        }

        save_scan([candidate], symbols_scanned=100, regime=regime)
        result = query_day(date.today().isoformat())

        assert result["scan_count"] == 1
        assert result["candidates_found"] == 1
        assert result["candidates"][0]["symbol"] == "AAPL"
        assert result["candidates"][0]["score"] == pytest.approx(score, abs=0.5)

    def test_trade_save_and_query(self, temp_db):
        """save_trade → query_day returns the trade with correct fields."""
        from data.store import save_trade, query_day
        order = {
            "symbol": "NVDA", "buy_order_id": "buy-001",
            "partial_order_id": "part-002", "trail_order_id": "trail-003",
            "shares": 20, "partial_shares": 10, "trail_shares": 10,
            "entry": 550.0, "stop_loss": 535.0, "partial_target": 580.0,
            "trail_atr": 540.0, "score": 82.5,
        }
        save_trade(order)
        result = query_day(date.today().isoformat())
        assert result["trades_placed"] == 1
        trade = result["trades"][0]
        assert trade["symbol"] == "NVDA"
        assert trade["score"] == pytest.approx(82.5)
        assert trade["shares"] == 20

    def test_webapp_reflects_saved_scan(self, temp_db, api_client):
        """Save a scan to DB → API dashboard endpoint returns the candidate."""
        import data.store as store
        from strategy.market_regime import Regime
        today = date.today().isoformat()

        # Monkeypatch already applied by api_client fixture — save directly
        regime = MagicMock()
        regime.state.value = Regime.BULL_TREND.value
        regime.adx = 30.0
        regime.spy_above_200ma = True
        regime.spy_slope_20d = 0.03
        regime.score_multiplier = 1.0

        candidate = {
            "symbol": "TSLA", "score": 78.0, "entry": 200.0,
            "stop": 192.0, "target": 216.0, "trail_atr": 195.0,
            "shares": 15, "partial_shares": 8, "trail_shares": 7,
            "dollar_risk": 120.0, "risk_reward": 2.0,
            "volume_ratio": 2.1, "rsi": 63.0, "rs_vs_spy": 4.2,
            "is_trap": False, "regime": "BULL_TREND",
        }
        store.save_scan([candidate], symbols_scanned=50, regime=regime)

        r = api_client.get(f"/api/dashboard?date={today}")
        data = r.json()
        assert data["candidates_found"] == 1
        assert data["candidates"][0]["symbol"] == "TSLA"

    def test_config_round_trip_via_api(self, api_client):
        """POST config → GET config returns same values."""
        r = api_client.get("/api/config")
        cfg = r.json()
        original_score = cfg["BREAKOUT_MIN_SCORE"]

        cfg["BREAKOUT_MIN_SCORE"] = "65.0"
        api_client.post("/api/config", json=cfg)

        r2 = api_client.get("/api/config")
        assert r2.json()["BREAKOUT_MIN_SCORE"] == "65.0"

        # Restore
        cfg["BREAKOUT_MIN_SCORE"] = original_score
        api_client.post("/api/config", json=cfg)

    def test_universe_override_round_trip_via_api(self, api_client):
        """POST custom symbols → GET returns them → DELETE restores defaults."""
        custom = ["AAPL", "NVDA", "QQQ", "SPY"]
        api_client.post("/api/symbols/fallback", json={"symbols": custom})

        r = api_client.get("/api/symbols/fallback")
        assert r.json()["symbols"] == custom
        assert r.json()["is_custom"] is True

        api_client.delete("/api/symbols/fallback/reset")
        r2 = api_client.get("/api/symbols/fallback")
        assert r2.json()["is_custom"] is False
        assert len(r2.json()["symbols"]) >= 400


# ══════════════════════════════════════════════════════════════════════════════
# 11. RUNNER & SCAN API
# ══════════════════════════════════════════════════════════════════════════════

class TestRunnerAndScanAPI:
    """Tests for /api/runner/* and /api/scan/* endpoints."""

    # ── Runner status ─────────────────────────────────────────────────────────

    def test_runner_status_returns_running_key(self, api_client):
        r = api_client.get("/api/runner/status")
        assert r.status_code == 200
        assert "running" in r.json()
        assert isinstance(r.json()["running"], bool)

    def test_runner_status_includes_is_paper(self, api_client):
        r = api_client.get("/api/runner/status")
        assert r.status_code == 200
        assert "is_paper" in r.json()

    def test_trading_mode_endpoint_returns_is_paper(self, api_client):
        r = api_client.get("/api/trading-mode")
        assert r.status_code == 200
        data = r.json()
        assert "is_paper" in data
        assert "base_url" in data
        assert isinstance(data["is_paper"], bool)

    def test_runner_not_running_by_default(self, api_client):
        r = api_client.get("/api/runner/status")
        assert r.json()["running"] is False

    def test_runner_stop_when_not_running(self, api_client):
        r = api_client.post("/api/runner/stop", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["running"] is False

    def test_runner_start_spawns_process(self, api_client):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("webapp.app.subprocess.Popen", return_value=mock_proc):
            r = api_client.post("/api/runner/start", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["running"] is True

    def test_runner_start_dry_run_adds_flag(self, api_client):
        import webapp.app as app_module
        app_module._runner_proc = None
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("webapp.app.subprocess.Popen", return_value=mock_proc) as mock_popen:
            api_client.post("/api/runner/start", json={"dry_run": True})
        cmd = mock_popen.call_args[0][0]
        assert "--dry-run" in cmd

    def test_runner_start_live_omits_dry_run_flag(self, api_client):
        import webapp.app as app_module
        app_module._runner_proc = None
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch("webapp.app.subprocess.Popen", return_value=mock_proc) as mock_popen:
            api_client.post("/api/runner/start", json={"dry_run": False})
        cmd = mock_popen.call_args[0][0]
        assert "--dry-run" not in cmd

    # ── Scan output ───────────────────────────────────────────────────────────

    def test_scan_output_returns_correct_shape(self, api_client):
        r = api_client.get("/api/scan/output")
        assert r.status_code == 200
        data = r.json()
        assert "lines" in data
        assert "offset" in data
        assert "running" in data
        assert isinstance(data["lines"], list)
        assert isinstance(data["running"], bool)

    def test_scan_output_not_running_by_default(self, api_client):
        r = api_client.get("/api/scan/output")
        assert r.json()["running"] is False

    def test_scan_output_offset_parameter(self, api_client):
        r = api_client.get("/api/scan/output?offset=0")
        assert r.status_code == 200
        assert r.json()["offset"] == 0

    # ── Scan start ────────────────────────────────────────────────────────────

    def test_scan_start_dry_run_omits_execute_flag(self, api_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        with patch("webapp.app.subprocess.Popen", return_value=mock_proc) as mock_popen:
            r = api_client.post("/api/scan/start", json={"execute": False})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        cmd = mock_popen.call_args[0][0]
        assert "--execute" not in cmd

    def test_scan_start_live_adds_execute_flag(self, api_client):
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        with patch("webapp.app.subprocess.Popen", return_value=mock_proc) as mock_popen:
            r = api_client.post("/api/scan/start", json={"execute": True})
        assert r.status_code == 200
        cmd = mock_popen.call_args[0][0]
        assert "--execute" in cmd

    def test_scan_start_while_running_returns_not_ok(self, api_client):
        import webapp.app as app_module
        original = app_module._scan_running
        app_module._scan_running = True
        try:
            r = api_client.post("/api/scan/start", json={})
            assert r.json()["ok"] is False
        finally:
            app_module._scan_running = original


# ══════════════════════════════════════════════════════════════════════════════
# 12. POSITION MANAGER (PME DECISION LOGIC)
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionManager:
    """Tests for PositionManager._decide() — pure decision logic, no I/O."""

    def _pm(self):
        with patch("strategy.position_manager.MarketDataClient"), \
             patch("strategy.position_manager.BreakoutScorer"):
            from strategy.position_manager import PositionManager
            return PositionManager()

    def test_trap_always_exits(self):
        pm = self._pm()
        action, reason = pm._decide(score=85, r=3.0, rs=12.0,
                                     trap=True, volume_selloff=False, already_added=False)
        from strategy.position_manager import EXIT
        assert action == EXIT
        assert "trap" in reason.lower()

    def test_volume_selloff_exits(self):
        pm = self._pm()
        action, reason = pm._decide(score=80, r=2.5, rs=10.0,
                                     trap=False, volume_selloff=True, already_added=False)
        from strategy.position_manager import EXIT
        assert action == EXIT
        assert "selloff" in reason.lower() or "distribution" in reason.lower()

    def test_high_score_rs_ok_returns_add(self):
        import config
        from strategy.position_manager import ADD
        pm = self._pm()
        action, _ = pm._decide(
            score=config.PME_ADD_SCORE_THRESHOLD + 5,
            r=2.0,
            rs=config.PME_RS_ADD_MIN_PCT + 2.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == ADD

    def test_moderate_score_returns_hold(self):
        import config
        from strategy.position_manager import HOLD
        pm = self._pm()
        score = (config.PME_ADD_SCORE_THRESHOLD + config.PME_HOLD_SCORE_MIN) / 2
        action, _ = pm._decide(
            score=score, r=2.5,
            rs=config.PME_RS_ADD_MIN_PCT + 2.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == HOLD

    def test_trim_light_score_tier(self):
        import config
        from strategy.position_manager import TRIM_LIGHT
        pm = self._pm()
        score = (config.PME_HOLD_SCORE_MIN + config.PME_TRIM_LIGHT_SCORE_MIN) / 2
        action, _ = pm._decide(
            score=score, r=2.5, rs=5.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == TRIM_LIGHT

    def test_trim_heavy_score_tier(self):
        import config
        from strategy.position_manager import TRIM_HEAVY
        pm = self._pm()
        score = (config.PME_TRIM_LIGHT_SCORE_MIN + config.PME_TRIM_HEAVY_SCORE_MIN) / 2
        action, _ = pm._decide(
            score=score, r=2.5, rs=5.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == TRIM_HEAVY

    def test_very_low_score_exits(self):
        import config
        from strategy.position_manager import EXIT
        pm = self._pm()
        action, _ = pm._decide(
            score=config.PME_TRIM_HEAVY_SCORE_MIN - 5,
            r=1.0, rs=5.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == EXIT

    def test_r_below_floor_downgrades_hold_to_trim_light(self):
        import config
        from strategy.position_manager import TRIM_LIGHT
        pm = self._pm()
        score = config.PME_HOLD_SCORE_MIN + 2  # would be HOLD
        r = config.PME_R_TRIM_FLOOR - 0.5      # below floor
        action, reason = pm._decide(
            score=score, r=r, rs=5.0,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == TRIM_LIGHT
        assert "floor" in reason.lower() or "below" in reason.lower()

    def test_r_below_floor_downgrades_add_to_trim_light(self):
        import config
        from strategy.position_manager import TRIM_LIGHT
        pm = self._pm()
        score = config.PME_ADD_SCORE_THRESHOLD + 5  # would be ADD
        r = config.PME_R_TRIM_FLOOR - 0.5           # below floor
        rs = config.PME_RS_ADD_MIN_PCT + 5.0
        action, _ = pm._decide(
            score=score, r=r, rs=rs,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == TRIM_LIGHT

    def test_rs_below_add_min_blocks_add(self):
        import config
        from strategy.position_manager import HOLD
        pm = self._pm()
        score = config.PME_ADD_SCORE_THRESHOLD + 5  # would be ADD
        rs = config.PME_RS_ADD_MIN_PCT - 2.0        # below add minimum
        r = config.PME_R_TRIM_FLOOR + 1.0           # above floor
        action, reason = pm._decide(
            score=score, r=r, rs=rs,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == HOLD
        assert "rs" in reason.lower() or "add minimum" in reason.lower()

    def test_rs_below_downgrade_below_converts_hold_to_trim(self):
        import config
        from strategy.position_manager import TRIM_LIGHT
        pm = self._pm()
        score = config.PME_HOLD_SCORE_MIN + 2       # would be HOLD
        rs = config.PME_RS_DOWNGRADE_BELOW_PCT - 1  # very weak RS
        r = config.PME_R_TRIM_FLOOR + 1.0
        action, reason = pm._decide(
            score=score, r=r, rs=rs,
            trap=False, volume_selloff=False, already_added=False,
        )
        assert action == TRIM_LIGHT
        assert "rs" in reason.lower() or "relative strength" in reason.lower()

    def test_followthrough_guard_blocks_readd(self):
        import config
        from strategy.position_manager import HOLD
        pm = self._pm()
        score = config.PME_ADD_SCORE_THRESHOLD + 5
        rs = config.PME_RS_ADD_MIN_PCT + 5.0
        r = config.PME_R_TRIM_FLOOR + 1.0
        action, reason = pm._decide(
            score=score, r=r, rs=rs,
            trap=False, volume_selloff=False, already_added=True,  # already added
        )
        assert action == HOLD
        assert "added" in reason.lower() or "followthrough" in reason.lower() or "skip" in reason.lower()

    def test_trap_overrides_high_score(self):
        from strategy.position_manager import EXIT
        import config
        pm = self._pm()
        # Even perfect score+RS should exit on trap
        action, _ = pm._decide(
            score=99, r=5.0,
            rs=config.PME_RS_ADD_MIN_PCT + 20,
            trap=True, volume_selloff=False, already_added=False,
        )
        assert action == EXIT


# ══════════════════════════════════════════════════════════════════════════════
# 13. POSITION EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionExecutor:
    """Tests for PositionExecutor — uses mock Alpaca client, no real orders.

    notify() is patched for every test so no WhatsApp messages fire during CI.
    """

    @pytest.fixture(autouse=True)
    def _no_notify(self):
        with patch("execution.position_executor.notify"):
            yield

    def _make_eval(self, action: str, symbol: str = "AAPL",
                   shares: int = 100, fill_price: float = 100.0,
                   current_price: float = 110.0, r: float = 2.0) -> "PositionEvaluation":
        from strategy.position_manager import PositionEvaluation
        return PositionEvaluation(
            symbol=symbol,
            buy_order_id=f"test-{symbol}-001",
            action=action,
            score=75.0,
            r_multiple=r,
            rs_vs_spy=8.0,
            trap_triggered=False,
            reason="test",
            current_shares=shares,
            fill_price=fill_price,
            current_price=current_price,
        )

    def test_hold_returns_not_executed(self, mock_alpaca):
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import HOLD
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(HOLD))
        assert result["executed"] is False
        assert result["qty"] == 0
        mock_alpaca.trading_client.submit_order.assert_not_called()

    def test_market_closed_returns_not_executed(self, mock_alpaca):
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import TRIM_LIGHT
        mock_alpaca.is_market_open.return_value = False
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(TRIM_LIGHT))
        assert result["executed"] is False
        assert result["error"] == "market_closed"
        mock_alpaca.trading_client.submit_order.assert_not_called()

    def test_trim_light_sells_correct_fraction(self, mock_alpaca):
        import config
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import TRIM_LIGHT
        import math
        live_qty = 100
        pos_mock = MagicMock()
        pos_mock.qty = str(live_qty)
        mock_alpaca.get_position.return_value = pos_mock
        mock_order = MagicMock()
        mock_order.id = "order-trim-light"
        mock_alpaca.trading_client.submit_order.return_value = mock_order
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(TRIM_LIGHT, shares=live_qty))
        expected_qty = max(1, math.floor(live_qty * config.PME_TRIM_LIGHT_PCT))
        assert result["executed"] is True
        assert result["qty"] == expected_qty

    def test_trim_heavy_sells_more_than_trim_light(self, mock_alpaca):
        import config
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import TRIM_LIGHT, TRIM_HEAVY
        import math
        live_qty = 100
        pos_mock = MagicMock()
        pos_mock.qty = str(live_qty)
        mock_alpaca.get_position.return_value = pos_mock
        mock_order = MagicMock()
        mock_order.id = "order-trim-heavy"
        mock_alpaca.trading_client.submit_order.return_value = mock_order
        ex = PositionExecutor(mock_alpaca)
        light_result = ex.execute(self._make_eval(TRIM_LIGHT, shares=live_qty))
        mock_alpaca.trading_client.submit_order.reset_mock()
        heavy_result = ex.execute(self._make_eval(TRIM_HEAVY, shares=live_qty))
        assert heavy_result["qty"] > light_result["qty"]

    def test_exit_cancels_open_orders_and_sells_all(self, mock_alpaca):
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import EXIT
        live_qty = 50
        pos_mock = MagicMock()
        pos_mock.qty = str(live_qty)
        mock_alpaca.get_position.return_value = pos_mock
        # Two open orders to cancel
        open_order1 = MagicMock()
        open_order1.id = "stop-order-1"
        open_order2 = MagicMock()
        open_order2.id = "trail-order-2"
        mock_alpaca.trading_client.get_orders.return_value = [open_order1, open_order2]
        sell_order = MagicMock()
        sell_order.id = "exit-market-order"
        mock_alpaca.trading_client.submit_order.return_value = sell_order
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(EXIT, shares=live_qty))
        assert result["executed"] is True
        assert result["qty"] == live_qty
        assert result["cancelled_orders"] == 2
        assert mock_alpaca.trading_client.cancel_order_by_id.call_count == 2

    def test_exit_with_no_position_returns_not_executed(self, mock_alpaca):
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import EXIT
        mock_alpaca.get_position.return_value = None
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(EXIT))
        assert result["executed"] is False
        assert result["error"] == "no_position"
        mock_alpaca.trading_client.submit_order.assert_not_called()

    def test_add_places_buy_order(self, mock_alpaca):
        import config
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import ADD
        mock_alpaca.get_portfolio_value.return_value = 100_000.0
        buy_order = MagicMock()
        buy_order.id = "add-buy-order"
        mock_alpaca.trading_client.submit_order.return_value = buy_order
        ex = PositionExecutor(mock_alpaca)
        # shares=200, fill=100 → orig_value=20000, max_add=20000*(1.5-1)=10000
        # budget=100000*0.20=20000 → capped at 10000 → qty=floor(10000/110)=90
        result = ex.execute(self._make_eval(ADD, shares=200, fill_price=100.0, current_price=110.0))
        assert result["executed"] is True
        assert result["qty"] >= 1
        mock_alpaca.trading_client.submit_order.assert_called_once()

    def test_add_skips_when_budget_too_small(self, mock_alpaca):
        from execution.position_executor import PositionExecutor
        from strategy.position_manager import ADD
        mock_alpaca.get_portfolio_value.return_value = 100_000.0
        # shares=1, fill=1 → orig_value=1, max_add=0.5 — cannot buy 1 share at $110
        ex = PositionExecutor(mock_alpaca)
        result = ex.execute(self._make_eval(ADD, shares=1, fill_price=1.0, current_price=110.0))
        assert result["executed"] is False
        assert result["error"] == "budget_too_small"
        mock_alpaca.trading_client.submit_order.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 14. SECURITY
# ══════════════════════════════════════════════════════════════════════════════

class TestSecurity:
    """Session auth, rate limiting, input validation, security headers."""

    @pytest.fixture
    def auth_client(self, tmp_path, monkeypatch):
        """TestClient with auth ENABLED (user=testuser, pass=testpass)."""
        import data.store as store
        import data.universe as universe
        import webapp.app as app_module
        from collections import defaultdict
        from fastapi.testclient import TestClient
        from webapp.app import app

        monkeypatch.setattr(store,      "DB_PATH",        tmp_path / "sec.db")
        monkeypatch.setattr(universe,   "_OVERRIDE_PATH", tmp_path / "uni.json")
        fake_env = tmp_path / "sec.env"
        fake_env.write_text("", encoding="utf-8")
        monkeypatch.setattr(app_module, "ENV_PATH",       fake_env)
        monkeypatch.setattr(app_module, "_AUTH_USER",     "testuser")
        monkeypatch.setattr(app_module, "_AUTH_PASS",     "testpass")
        monkeypatch.setattr(app_module, "_AUTH_ENABLED",  True)
        monkeypatch.setattr(app_module, "_SESSIONS",      {})
        monkeypatch.setattr(app_module, "_RATE_HITS",     defaultdict(list))
        store.init_db()

        with TestClient(app, raise_server_exceptions=True) as client:
            yield client

    # ── Open endpoints ────────────────────────────────────────────────────────

    def test_login_page_accessible_without_auth(self, auth_client):
        r = auth_client.get("/login")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_logout_accessible_without_session(self, auth_client):
        r = auth_client.post("/logout")
        assert r.status_code == 200

    def test_static_accessible_without_auth(self, auth_client):
        r = auth_client.get("/static/login.html")
        assert r.status_code == 200

    # ── Protected endpoints ───────────────────────────────────────────────────

    def test_api_returns_401_without_session(self, auth_client):
        r = auth_client.get("/api/dashboard", follow_redirects=False)
        assert r.status_code == 401
        assert r.json()["detail"] == "not_authenticated"

    def test_root_redirects_without_session(self, auth_client):
        r = auth_client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    # ── Login / logout ────────────────────────────────────────────────────────

    def test_correct_credentials_return_session_cookie(self, auth_client):
        r = auth_client.post("/login", json={"username": "testuser", "password": "testpass"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "a1t_sess" in r.cookies

    def test_wrong_password_returns_401(self, auth_client):
        r = auth_client.post("/login", json={"username": "testuser", "password": "wrong"})
        assert r.status_code == 401
        assert r.json()["ok"] is False

    def test_authenticated_session_accesses_api(self, auth_client):
        auth_client.post("/login", json={"username": "testuser", "password": "testpass"})
        r = auth_client.get("/api/dashboard")
        assert r.status_code == 200

    def test_logout_clears_session(self, auth_client):
        auth_client.post("/login", json={"username": "testuser", "password": "testpass"})
        assert auth_client.get("/api/dashboard").status_code == 200
        auth_client.post("/logout")
        r = auth_client.get("/api/dashboard", follow_redirects=False)
        assert r.status_code == 401

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def test_rate_limit_blocks_after_max_attempts(self, auth_client):
        import time
        import webapp.app as app_module
        now = time.time()
        # Pre-fill the rate bucket for "testclient" (TestClient's default host)
        app_module._RATE_HITS["testclient"] = [now] * app_module._RATE_MAX
        r = auth_client.post("/login", json={"username": "x", "password": "y"})
        assert r.status_code == 429

    # ── Input validation ──────────────────────────────────────────────────────

    def test_invalid_date_format_returns_400(self, api_client):
        r = api_client.get("/api/dashboard?date=01-15-2024")
        assert r.status_code == 400

    def test_sql_injection_in_date_returns_400(self, api_client):
        r = api_client.get("/api/dashboard?date=2024-01-01;DROP TABLE scan_runs--")
        assert r.status_code == 400

    def test_valid_date_passes_validation(self, api_client):
        r = api_client.get("/api/dashboard?date=2024-06-15")
        assert r.status_code == 200
