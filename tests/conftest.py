"""
Shared fixtures for ATrades regression tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# Make project root importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ── OHLCV data generators ─────────────────────────────────────────────────────

def make_ohlcv(
    bars: int = 100,
    base_price: float = 60.0,
    avg_vol: int = 2_000_000,
    trend: float = 0.003,
    seed: int = 42,
) -> pd.DataFrame:
    """Generic OHLCV DataFrame on business days ending 2025-01-31."""
    np.random.seed(seed)
    dates  = pd.bdate_range(end="2025-01-31", periods=bars)
    daily  = np.random.normal(trend, 0.008, bars)
    close  = base_price * np.cumprod(1 + daily)
    noise  = np.abs(np.random.normal(0, 0.005, bars))
    return pd.DataFrame({
        "open":   close * (1 - noise * 0.5),
        "high":   close * (1 + noise),
        "low":    close * (1 - noise),
        "close":  close,
        "volume": np.full(bars, avg_vol, dtype=float),
    }, index=dates)


def make_breakout_df(bars: int = 100) -> pd.DataFrame:
    """OHLCV that should trigger all breakout signals on the last bar."""
    df = make_ohlcv(bars, base_price=60.0, avg_vol=2_000_000, trend=0.003)
    prior_20d_high = df["close"].iloc[-21:-1].max()
    # Last bar: close 3% above 20d high with volume surge
    df.iloc[-1, df.columns.get_loc("close")] = prior_20d_high * 1.03
    df.iloc[-1, df.columns.get_loc("high")]  = prior_20d_high * 1.035
    df.iloc[-1, df.columns.get_loc("open")]  = prior_20d_high * 1.01
    df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000  # 2.5x surge
    return df


def make_spy_df(bars: int = 100) -> pd.DataFrame:
    """Flat/slightly bullish SPY so stock shows positive relative strength."""
    return make_ohlcv(bars, base_price=400.0, avg_vol=80_000_000, trend=0.001, seed=99)


def make_bull_spy_df(bars: int = 100) -> pd.DataFrame:
    """SPY in a strong bull trend (above 200MA, ADX high, positive slope)."""
    np.random.seed(7)
    dates = pd.bdate_range(end="2025-01-31", periods=bars)
    close = 400.0 * np.cumprod(1 + np.random.normal(0.004, 0.006, bars))
    noise = np.abs(np.random.normal(0, 0.004, bars))
    return pd.DataFrame({
        "open":   close * (1 - noise * 0.5),
        "high":   close * (1 + noise),
        "low":    close * (1 - noise),
        "close":  close,
        "volume": np.full(bars, 80_000_000, dtype=float),
    }, index=dates)


def make_bear_spy_df(bars: int = 100) -> pd.DataFrame:
    """SPY in a bear trend (below 200MA, negative slope)."""
    np.random.seed(13)
    dates = pd.bdate_range(end="2025-01-31", periods=bars)
    close = 400.0 * np.cumprod(1 + np.random.normal(-0.005, 0.010, bars))
    noise = np.abs(np.random.normal(0, 0.005, bars))
    return pd.DataFrame({
        "open":   close * (1 - noise * 0.5),
        "high":   close * (1 + noise),
        "low":    close * (1 - noise),
        "close":  close,
        "volume": np.full(bars, 80_000_000, dtype=float),
    }, index=dates)


# ── Mock BreakoutSignals builder ───────────────────────────────────────────────

def make_signals(
    symbol: str = "AAPL",
    price: float = 100.0,
    atr: float = 2.0,
    support: float = 95.0,
    all_triggered: bool = True,
) -> "BreakoutSignals":
    from strategy.breakout_signals import BreakoutSignals, SignalResult

    def sig(triggered: bool, value: float = 1.0) -> SignalResult:
        return SignalResult(triggered=triggered, value=value)

    return BreakoutSignals(
        symbol=symbol,
        current_price=price,
        current_volume=3_000_000,
        avg_volume_20d=2_000_000,
        atr_14=atr,
        support_level=support,
        breakout_20d=sig(all_triggered, 0.025),
        breakout_50d=sig(all_triggered, 0.015),
        consolidation=sig(all_triggered, 0.012),
        higher_lows=sig(all_triggered, 1.0),
        volume_surge=sig(all_triggered, 2.5),
        rsi_zone=sig(all_triggered, 62.0),
        relative_strength=sig(all_triggered, 5.0),
        atr_expansion=sig(all_triggered, 1.35),
        earnings_proximity=sig(False, 0.0),
        accumulation=None,
        bull_trap=None,
    )


# ── Mock Alpaca client ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_alpaca():
    client = MagicMock()
    client.get_portfolio_value.return_value = 100_000.0
    client.get_cash.return_value = 50_000.0
    client.get_position.return_value = None
    client.get_all_positions.return_value = []
    client.is_market_open.return_value = True
    return client


# ── Temp DB fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Redirect the store's DB_PATH to a temp file and initialise schema."""
    import data.store as store
    db_file = tmp_path / "test_atrades.db"
    monkeypatch.setattr(store, "DB_PATH", db_file)
    store.init_db()
    return db_file


# ── Temp universe override fixture ────────────────────────────────────────────

@pytest.fixture
def temp_universe(tmp_path, monkeypatch):
    """Redirect _OVERRIDE_PATH to a temp dir so tests don't touch real file."""
    import data.universe as universe
    override = tmp_path / "universe_override.json"
    monkeypatch.setattr(universe, "_OVERRIDE_PATH", override)
    return override


# ── FastAPI test client ────────────────────────────────────────────────────────

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """TestClient with isolated DB and no real Alpaca calls."""
    import data.store as store
    import data.universe as universe
    from fastapi.testclient import TestClient
    from webapp.app import app

    # Isolate DB
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test_api.db")
    # Isolate universe override
    monkeypatch.setattr(universe, "_OVERRIDE_PATH", tmp_path / "universe_override.json")

    with TestClient(app) as client:
        yield client
