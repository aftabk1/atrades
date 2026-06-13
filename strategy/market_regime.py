"""
Market regime detector.

Classifies the current market environment into one of four states using
SPY daily data. The regime gates and adjusts the scanner output:

  BULL_TREND      SPY > 200MA, ADX ≥ 25, positive slope — full scanning
  SIDEWAYS        Mixed signals, low directional conviction — scan with stricter score floor
  BEAR_TREND      SPY < 200MA or rapid decline — scan with heavy penalty, warn user
  HIGH_VOLATILITY Annualised realised vol > 30% — suspend scanning, warn user

Score multiplier: regime.score_multiplier × raw_score (applied in scanner.py, not here).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd
from loguru import logger

import config


class Regime(str, Enum):
    BULL_TREND      = "BULL_TREND"
    SIDEWAYS        = "SIDEWAYS"
    BEAR_TREND      = "BEAR_TREND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


@dataclass
class MarketRegime:
    state: Regime
    adx: float                  # ADX(14) of SPY — directional conviction
    spy_above_200ma: bool
    spy_slope_20d: float        # SPY 20-day return %
    spy_slope_50d: float        # SPY 50-day return %
    realized_vol_20d: float     # Annualised realised vol % of SPY
    score_multiplier: float     # multiply candidate scores by this
    min_score_override: float   # scanner uses max(config_floor, this)
    scan_recommended: bool
    description: str


# ── Regime thresholds ─────────────────────────────────────────────────────────
_ADX_TRENDING   = 25.0     # ADX ≥ 25 = trending
_ADX_CHOPPY     = 18.0     # ADX < 18 = choppy / no trend
_VOL_HIGH       = 30.0     # annualised realised vol % that triggers HIGH_VOLATILITY
_SLOPE_BULL     =  2.0     # SPY 20d return % minimum for bull regime
_SLOPE_BEAR     = -3.0     # SPY 20d return % threshold for bear
_MA_LOOKBACK    = 200      # bars for long-term MA

_REGIME_CONFIG: dict[Regime, dict] = {
    Regime.BULL_TREND:      {"multiplier": 1.00, "min_score": 40.0, "scan": True},
    Regime.SIDEWAYS:        {"multiplier": 0.80, "min_score": 52.0, "scan": True},
    Regime.BEAR_TREND:      {"multiplier": 0.55, "min_score": 62.0, "scan": False},
    Regime.HIGH_VOLATILITY: {"multiplier": 0.40, "min_score": 70.0, "scan": False},
}

_REGIME_LABELS = {
    Regime.BULL_TREND:      "🟢 BULL TREND",
    Regime.SIDEWAYS:        "🟡 SIDEWAYS",
    Regime.BEAR_TREND:      "🔴 BEAR TREND",
    Regime.HIGH_VOLATILITY: "⚠️  HIGH VOLATILITY",
}


def detect_regime(spy_df: pd.DataFrame) -> MarketRegime:
    """
    Classify the current market regime from SPY daily OHLCV data.
    Requires at least 60 bars; falls back to SIDEWAYS with a warning.
    """
    override = getattr(config, "REGIME_OVERRIDE", "")
    if override and override in {r.value for r in Regime}:
        logger.info(f"Market regime: OVERRIDE → {override}")
        return _make_regime(Regime(override), 0.0, True, 0.0, 0.0, 0.0)

    if spy_df is None or len(spy_df) < 60:
        logger.warning("Insufficient SPY data — defaulting to SIDEWAYS regime")
        return _make_regime(Regime.SIDEWAYS, 0.0, True, 0.0, 0.0, 0.0)

    close = spy_df["close"]
    n     = len(close)

    # ── ADX (directional conviction) ──────────────────────────────────────────
    adx_val = float(_adx(spy_df, 14).iloc[-1])

    # ── Trend slope ───────────────────────────────────────────────────────────
    slope_20d = float((close.iloc[-1] / close.iloc[min(-21, -n)] - 1) * 100)
    slope_50d = float((close.iloc[-1] / close.iloc[min(-51, -n)] - 1) * 100)

    # ── 200-day MA ────────────────────────────────────────────────────────────
    ma200      = float(close.rolling(_MA_LOOKBACK, min_periods=60).mean().iloc[-1])
    above_200  = float(close.iloc[-1]) > ma200

    # ── Realised volatility ───────────────────────────────────────────────────
    ret_20     = close.pct_change().dropna().iloc[-20:]
    real_vol   = float(ret_20.std() * np.sqrt(252) * 100)

    # ── Classification ────────────────────────────────────────────────────────
    state = _classify(adx_val, above_200, slope_20d, real_vol)

    regime = _make_regime(state, adx_val, above_200, slope_20d, slope_50d, real_vol)
    logger.info(f"Market regime: {_REGIME_LABELS.get(state, state)} — {regime.description}")
    return regime


# ── Helpers ───────────────────────────────────────────────────────────────────

_SLOPE_RAPID_DECLINE = -7.0   # SPY falling >7% in 20d = bear even if above 200MA

def _classify(
    adx: float, above_200ma: bool, slope_20d: float, real_vol: float
) -> Regime:
    if real_vol > _VOL_HIGH:
        return Regime.HIGH_VOLATILITY
    if not above_200ma and slope_20d < _SLOPE_BEAR:
        return Regime.BEAR_TREND
    # Rapid decline — even if technically above 200MA, a -7%+ 20-day drop signals
    # distribution / institutional selling; treat as bear to block new entries.
    if slope_20d < _SLOPE_RAPID_DECLINE:
        return Regime.BEAR_TREND
    if above_200ma and adx >= _ADX_TRENDING and slope_20d >= _SLOPE_BULL:
        return Regime.BULL_TREND
    return Regime.SIDEWAYS


def _make_regime(
    state: Regime,
    adx: float,
    above_200: bool,
    slope_20d: float,
    slope_50d: float,
    real_vol: float,
) -> MarketRegime:
    cfg = _REGIME_CONFIG[state]
    desc = (
        f"SPY {'above' if above_200 else 'below'} 200MA | "
        f"ADX={adx:.1f} | "
        f"Slope20d={slope_20d:+.1f}% | "
        f"Slope50d={slope_50d:+.1f}% | "
        f"RealVol={real_vol:.1f}%"
    )
    return MarketRegime(
        state=state,
        adx=adx,
        spy_above_200ma=above_200,
        spy_slope_20d=slope_20d,
        spy_slope_50d=slope_50d,
        realized_vol_20d=real_vol,
        score_multiplier=cfg["multiplier"],
        min_score_override=cfg["min_score"],
        scan_recommended=cfg["scan"],
        description=desc,
    )


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index using Wilder's smoothing."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    prev_h  = h.shift(1)
    prev_l  = l.shift(1)

    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)

    dm_plus  = (h - prev_h).clip(lower=0)
    dm_minus = (prev_l - l).clip(lower=0)

    # Where both DM are positive, zero out the smaller one
    both = (dm_plus > 0) & (dm_minus > 0)
    dm_plus  = dm_plus.where(~both | (dm_plus >= dm_minus), 0.0)
    dm_minus = dm_minus.where(~both | (dm_minus > dm_plus),  0.0)

    smooth = lambda s: s.ewm(span=period, adjust=False).mean()  # noqa: E731
    atr    = smooth(tr)
    di_pos = 100 * smooth(dm_plus)  / atr.replace(0, np.nan)
    di_neg = 100 * smooth(dm_minus) / atr.replace(0, np.nan)

    dx  = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, np.nan)
    adx = smooth(dx)
    return adx.fillna(0)
