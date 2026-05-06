"""
Multi-factor breakout signal detection.

Each detector returns a SignalResult(triggered, value, description).
`detect_all` orchestrates all checks and returns a BreakoutSignals bundle,
or None if the symbol fails base filters or lacks a price breakout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

import config

_MIN_BARS = 60  # minimum history needed for reliable signal detection


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    triggered: bool
    value: float       # raw metric: volume ratio, RSI, RS %, ATR ratio, etc.
    description: str = ""


def _default_signal() -> SignalResult:
    return SignalResult(False, 0.0)


@dataclass
class BreakoutSignals:
    symbol: str
    current_price: float
    current_volume: float
    avg_volume_20d: float
    atr_14: float = 0.0
    support_level: float = 0.0

    breakout_20d:       SignalResult = field(default_factory=_default_signal)
    breakout_10d:       SignalResult = field(default_factory=_default_signal)
    breakout_50d:       SignalResult = field(default_factory=_default_signal)
    consolidation:      SignalResult = field(default_factory=_default_signal)
    higher_lows:        SignalResult = field(default_factory=_default_signal)
    volume_surge:       SignalResult = field(default_factory=_default_signal)
    rsi_zone:           SignalResult = field(default_factory=_default_signal)
    relative_strength:  SignalResult = field(default_factory=_default_signal)
    atr_expansion:      SignalResult = field(default_factory=_default_signal)
    earnings_proximity: SignalResult = field(default_factory=_default_signal)

    gap_pct: float = 0.0       # today open vs prior close; ≥0.08 = gap-up breakout
    breakout_level: float = 0.0  # 20-day prior high — stored at trade entry for PME

    # ── Enhancement modules (populated after price-breakout gate) ─────────────
    # Type hints use strings to avoid circular imports at module load time.
    # Both are set by detect_all via lazy local imports.
    accumulation: "AccumulationSignals | None" = field(default=None)
    bull_trap:    "BullTrapResult | None"       = field(default=None)


# ── Entry point ───────────────────────────────────────────────────────────────

def detect_all(
    symbol: str,
    df: pd.DataFrame,
    spy_df: pd.DataFrame,
    earnings_date: Optional[datetime] = None,
    *,
    fast: bool = False,
    require_breakout: bool = True,
) -> Optional[BreakoutSignals]:
    """
    Run the full signal pipeline on a single symbol's daily OHLCV DataFrame.
    Returns None when the symbol fails base filters or has insufficient data.

    require_breakout=True (default): also returns None when no price breakout
    gate fires (used for new-entry scanning).
    require_breakout=False: skips the breakout gate — used by PME to re-score
    existing open positions that don't need to re-qualify as fresh breakouts.

    fast=True skips accumulation and bull-trap detection (optimizer mode).
    """
    if df is None or len(df) < _MIN_BARS:
        return None
    if not _passes_base_filters(df):
        return None

    close   = df["close"]
    volume  = df["volume"]
    current_price  = float(close.iloc[-1])
    avg_vol_20d    = float(volume.iloc[-21:-1].mean())
    atr14_val      = float(_atr(df, 14).iloc[-1])
    support        = _find_support(df)

    gap_pct = 0.0
    if len(df) >= 2:
        gap_pct = round(float(df["open"].iloc[-1] / df["close"].iloc[-2] - 1), 4)

    # 20-day prior high stored for PME breakout-level tracking
    breakout_level = float(close.iloc[-21:-1].max()) if len(close) >= 21 else 0.0

    sig = BreakoutSignals(
        symbol=symbol,
        current_price=current_price,
        current_volume=float(volume.iloc[-1]),
        avg_volume_20d=avg_vol_20d,
        atr_14=atr14_val,
        support_level=support,
        gap_pct=gap_pct,
        breakout_level=breakout_level,
    )

    sig.breakout_20d      = _check_price_breakout(df, 20)
    sig.breakout_10d      = _check_price_breakout(df, 10)
    sig.volume_surge      = _check_volume_surge(df)
    sig.relative_strength = _check_relative_strength(df, spy_df)

    if require_breakout:
        # Three-way entry gate — any one trigger qualifies the symbol:
        #   A: classic 20-day high breakout
        #   B: 10-day breakout + volume surge + relative strength (tight base thrust)
        #   C: earnings/news gap-up (≥ GAP_UP_THRESHOLD) + volume surge
        trigger_a = sig.breakout_20d.triggered
        trigger_b = (sig.breakout_10d.triggered
                     and sig.volume_surge.triggered
                     and sig.relative_strength.triggered)
        trigger_c = (gap_pct >= config.GAP_UP_THRESHOLD
                     and sig.volume_surge.triggered)
        if not (trigger_a or trigger_b or trigger_c):
            return None

    sig.breakout_50d      = _check_price_breakout(df, 50)
    sig.consolidation     = _check_consolidation(df)
    sig.higher_lows       = _check_higher_lows(df)
    sig.rsi_zone          = _check_rsi(df)
    sig.atr_expansion     = _check_atr_expansion(df)

    if earnings_date is not None:
        sig.earnings_proximity = _check_earnings_proximity(df, earnings_date)

    if not fast:
        # ── Institutional accumulation (skipped in fast/optimizer mode) ───────
        from .accumulation import detect_accumulation   # noqa: PLC0415
        sig.accumulation = detect_accumulation(df, lookback=config.ACCUM_LOOKBACK_DAYS)

        # ── Bull trap / false breakout detection ──────────────────────────────
        from .bull_trap import detect_bull_trap         # noqa: PLC0415
        sig.bull_trap = detect_bull_trap(df)

    return sig


# ── Base filters ──────────────────────────────────────────────────────────────

def _passes_base_filters(df: pd.DataFrame) -> bool:
    price      = float(df["close"].iloc[-1])
    avg_volume = float(df["volume"].iloc[-21:-1].mean())
    return price >= config.BREAKOUT_MIN_PRICE and avg_volume >= config.BREAKOUT_MIN_AVG_VOLUME


# ── Signal detectors ──────────────────────────────────────────────────────────

def _check_price_breakout(df: pd.DataFrame, window: int) -> SignalResult:
    """Close today > highest close of the prior `window` bars (no lookahead)."""
    close = df["close"]
    if len(close) < window + 1:
        return SignalResult(False, 0.0)

    current    = float(close.iloc[-1])
    prior_high = float(close.iloc[-(window + 1):-1].max())
    pct_above  = (current - prior_high) / prior_high * 100

    return SignalResult(
        triggered=current > prior_high,
        value=pct_above,
        description=f"{window}d breakout: {current:.2f} vs prior high {prior_high:.2f} ({pct_above:+.1f}%)",
    )


def _check_consolidation(df: pd.DataFrame) -> SignalResult:
    """
    Low daily-return volatility over the N bars preceding today signals a base.
    Std of daily returns < BREAKOUT_CONSOLIDATION_DAILY_VOL (default 1.5%) = consolidation.
    """
    n = config.BREAKOUT_CONSOLIDATION_LOOKBACK
    if len(df) < n + 5:
        return SignalResult(False, 0.0)

    # Exclude today's bar — we want pre-breakout behaviour
    prior_close = df["close"].iloc[-(n + 1):-1]
    daily_vol   = float(prior_close.pct_change().dropna().std())
    triggered   = daily_vol < config.BREAKOUT_CONSOLIDATION_DAILY_VOL

    return SignalResult(
        triggered=triggered,
        value=daily_vol,
        description=f"Consolidation: daily return σ={daily_vol:.3f} (threshold {config.BREAKOUT_CONSOLIDATION_DAILY_VOL})",
    )


def _check_higher_lows(df: pd.DataFrame) -> SignalResult:
    """Detect ascending swing lows over the prior N bars."""
    n    = config.BREAKOUT_HIGHER_LOWS_LOOKBACK
    lows = df["low"].iloc[-(n + 1):-1].values  # exclude today

    swing_lows = [
        lows[i]
        for i in range(1, len(lows) - 1)
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
    ]

    if len(swing_lows) < 2:
        return SignalResult(False, 0.0)

    ascending = all(swing_lows[i] > swing_lows[i - 1] for i in range(1, len(swing_lows)))
    return SignalResult(
        triggered=ascending,
        value=float(len(swing_lows)),
        description=f"Higher lows: {len(swing_lows)} swing lows, ascending={ascending}",
    )


def _check_volume_surge(df: pd.DataFrame) -> SignalResult:
    """Today's volume ≥ BREAKOUT_VOLUME_SURGE_MULT × 20-day average."""
    vol      = df["volume"]
    today    = float(vol.iloc[-1])
    avg_20   = float(vol.iloc[-21:-1].mean())

    if avg_20 == 0:
        return SignalResult(False, 0.0)

    ratio     = today / avg_20
    threshold = config.BREAKOUT_VOLUME_SURGE_MULT

    return SignalResult(
        triggered=ratio >= threshold,
        value=ratio,
        description=f"Volume surge: {ratio:.2f}x avg ({today:,.0f} vs 20d avg {avg_20:,.0f})",
    )


def _check_rsi(df: pd.DataFrame, period: int = 14) -> SignalResult:
    """RSI between BREAKOUT_RSI_LOW and BREAKOUT_RSI_HIGH (momentum zone, not overbought)."""
    close = df["close"]
    if len(close) < period + 1:
        return SignalResult(False, 0.0)

    rsi_val   = float(_rsi(close, period).iloc[-1])
    triggered = config.BREAKOUT_RSI_LOW <= rsi_val <= config.BREAKOUT_RSI_HIGH

    return SignalResult(
        triggered=triggered,
        value=rsi_val,
        description=f"RSI({period}): {rsi_val:.1f} (zone {config.BREAKOUT_RSI_LOW}–{config.BREAKOUT_RSI_HIGH})",
    )


def _check_relative_strength(
    df: pd.DataFrame, spy_df: pd.DataFrame, window: int = 20
) -> SignalResult:
    """Stock outperformed SPY over the past `window` trading days."""
    if spy_df is None or spy_df.empty:
        # Guard: skip rather than produce a spurious signal
        return SignalResult(False, 0.0, "RS: SPY data unavailable")

    if len(df) < window + 1 or len(spy_df) < window + 1:
        return SignalResult(False, 0.0)

    combined = (
        df["close"].rename("sym")
        .to_frame()
        .join(spy_df["close"].rename("spy"), how="inner")
    )
    if len(combined) < window + 1:
        return SignalResult(False, 0.0)

    sym_ret = float(combined["sym"].iloc[-1] / combined["sym"].iloc[-(window + 1)] - 1) * 100
    spy_ret = float(combined["spy"].iloc[-1] / combined["spy"].iloc[-(window + 1)] - 1) * 100
    rs      = sym_ret - spy_ret

    return SignalResult(
        triggered=rs > 0,
        value=rs,
        description=f"RS vs SPY ({window}d): {rs:+.1f}% (stock {sym_ret:+.1f}% / SPY {spy_ret:+.1f}%)",
    )


def _check_atr_expansion(df: pd.DataFrame) -> SignalResult:
    """
    Short-term ATR > long-term ATR by BREAKOUT_ATR_EXPANSION_THRESHOLD.
    Signals volatility contraction breaking out into expansion.
    """
    if len(df) < 25:
        return SignalResult(False, 0.0)

    atr5  = float(_atr(df, 5).iloc[-1])
    atr20 = float(_atr(df, 20).iloc[-1])

    if atr20 == 0:
        return SignalResult(False, 0.0)

    ratio     = atr5 / atr20
    threshold = config.BREAKOUT_ATR_EXPANSION_THRESHOLD

    return SignalResult(
        triggered=ratio > threshold,
        value=ratio,
        description=f"ATR expansion: {ratio:.2f}x (ATR5={atr5:.2f}, ATR20={atr20:.2f}, need >{threshold})",
    )


def _check_earnings_proximity(df: pd.DataFrame, earnings_date: object) -> SignalResult:
    """
    Bonus signal when within 5 days of earnings (post-earnings momentum
    or pre-earnings anticipation run).
    """
    try:
        earn_dt  = pd.to_datetime(earnings_date).tz_localize(None)
        last_dt  = pd.to_datetime(df.index[-1]).tz_localize(None)
        days_off = int((earn_dt - last_dt).days)

        if -5 <= days_off <= 5:
            return SignalResult(
                triggered=True,
                value=float(abs(days_off)),
                description=f"Earnings proximity: {days_off:+d} days to earnings",
            )
    except Exception:
        pass
    return SignalResult(False, 0.0)


# ── Calculation helpers ───────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    tr      = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss  = (-delta.clip(upper=0)).rolling(period, min_periods=1).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _find_support(df: pd.DataFrame, lookback: int = config.BREAKOUT_SUPPORT_LOOKBACK) -> float:
    """Lowest swing low in the prior `lookback` bars as the support reference."""
    lows = df["low"].iloc[-lookback:].values
    swing_lows = [
        lows[i]
        for i in range(1, len(lows) - 1)
        if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]
    ]
    return float(min(swing_lows)) if swing_lows else float(np.min(lows))


# Fix missing import referenced in _check_earnings_proximity
from datetime import datetime  # noqa: E402 — used in type hint only
