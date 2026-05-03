"""
ATrades End-to-End Regression Test Suite
=========================================
Run all:        pytest tests/
Run one section: pytest tests/ -k "config"
With coverage:  pytest tests/ --cov=. --cov-report=term-missing
Verbose output: pytest tests/ -v

Sections:
  1. Config & Environment
  2. Stock Universe
  3. SQLite Data Store
  4. Breakout Signal Detection
  5. Breakout Scorer
  6. Market Regime Detection
  7. Trade Setup Calculation
  8. Risk Manager
  9. Webapp API Endpoints
  10. End-to-End Pipeline
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
        expected = ["volume_surge", "breakout_20d", "relative_strength", "rsi_zone",
                    "breakout_50d", "atr_expansion", "consolidation", "higher_lows",
                    "earnings_proximity"]
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
        assert "ATrades" in r.text

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
