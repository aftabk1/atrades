"""
Bull-trap (false breakout) detector.

A bull trap occurs when price moves above a resistance level, attracts
buyers, then reverses sharply — trapping longs at the top.

Six warning signs are scored; hitting ≥ 40 points flags the breakout as
suspect and the scorer will apply a confidence penalty.

Warning signs:
  1. Weak close      — close in the lower 35% of today's bar range (selling into the move)
  2. Prior failures  — ≥ 2 past attempts at this level that reversed (proven resistance)
  3. Resistance zone — ≥ 4 prior highs clustered within 1.5% overhead (supply overhead)
  4. RSI divergence  — price makes higher high but RSI makes lower high (momentum fading)
  5. Narrow bar      — today's range < 70% of ATR(14) (low-conviction breakout thrust)
  6. Extension       — close > BREAKOUT_MAX_EXTENSION_PCT above the 20-day pivot (risk/reward gone)

Fully self-contained — no imports from other strategy modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
import pandas as pd

import config


class _Sig(NamedTuple):
    triggered: bool
    value: float
    description: str = ""


# Points per warning (base sum = 100; extension adds 20 → max 120)
_WEIGHTS: dict[str, int] = {
    "weak_close":         30,
    "prior_failures":     25,
    "resistance_zone":    20,
    "rsi_divergence":     15,
    "narrow_bar":         10,
    "extension":          20,   # 6th: price already extended too far from pivot
}

TRAP_THRESHOLD = 40   # trap_score ≥ this → flag as suspected trap


@dataclass
class BullTrapResult:
    trap_score: float = 0.0         # 0–120; higher = more likely a trap
    is_trap: bool = False
    warnings: list[str] = field(default_factory=list)

    weak_close:      _Sig = _Sig(False, 0.0)
    prior_failures:  _Sig = _Sig(False, 0.0)
    resistance_zone: _Sig = _Sig(False, 0.0)
    rsi_divergence:  _Sig = _Sig(False, 0.0)
    narrow_bar:      _Sig = _Sig(False, 0.0)
    extension:       _Sig = _Sig(False, 0.0)   # 6th: already extended above pivot
    extension_pct:   float = 0.0               # convenience: raw % above 20-day pivot


def detect_bull_trap(df: pd.DataFrame) -> BullTrapResult:
    """
    Analyse the current (last) bar for bull-trap characteristics.
    Returns BullTrapResult with aggregate trap_score and individual flags.
    """
    result = BullTrapResult()

    result.weak_close      = _check_weak_close(df)
    result.prior_failures  = _check_prior_failures(df)
    result.resistance_zone = _check_resistance_zone(df)
    result.rsi_divergence  = _check_rsi_divergence(df)
    result.narrow_bar      = _check_narrow_bar(df)
    result.extension       = _check_extension(df)
    result.extension_pct   = result.extension.value

    result.trap_score = float(
        sum(_WEIGHTS[k] for k in _WEIGHTS if getattr(result, k).triggered)
    )
    result.is_trap = result.trap_score >= config.BULL_TRAP_SCORE_THRESHOLD
    result.warnings = [
        getattr(result, k).description
        for k in _WEIGHTS
        if getattr(result, k).triggered
    ]

    return result


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_weak_close(df: pd.DataFrame) -> _Sig:
    """
    Strong breakouts close in the upper 60%+ of the day's range.
    Closing in the bottom 35% signals intraday rejection / selling pressure.
    """
    bar   = df.iloc[-1]
    h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
    rng   = h - l

    if rng <= 0:
        return _Sig(False, 0.0)

    position = (c - l) / rng   # 0 = at low, 1 = at high

    return _Sig(
        triggered=position < 0.35,
        value=position,
        description=(
            f"Weak close: closed at {position:.0%} of today's range "
            f"(${l:.2f}–${h:.2f}, close ${c:.2f}) — need >35%"
        ),
    )


def _check_prior_failures(
    df: pd.DataFrame,
    lookback: int = 60,
    proximity: float = 0.02,
    reversal_bars: int = 5,
) -> _Sig:
    """
    Count prior attempts to clear this price level that reversed within
    `reversal_bars` days. ≥ 2 proven failures = strong overhead resistance.
    """
    if len(df) < lookback + reversal_bars + 1:
        return _Sig(False, 0.0)

    current = float(df["close"].iloc[-1])
    history = df["close"].iloc[-(lookback + 1):-1].values

    failures = 0
    for i in range(len(history) - reversal_bars):
        if abs(history[i] - current) / current <= proximity:
            # Price was near this level — did it fail to hold within `reversal_bars`?
            future = history[i + 1 : i + 1 + reversal_bars]
            if len(future) > 0 and float(future.min()) < current * (1 - proximity):
                failures += 1

    return _Sig(
        triggered=failures >= 2,
        value=float(failures),
        description=(
            f"Prior failures at this level: {failures} "
            f"(within ±{proximity:.0%} of ${current:.2f}) — need <2 for clean break"
        ),
    )


def _check_resistance_zone(
    df: pd.DataFrame,
    lookback: int = 60,
    proximity: float = 0.015,
    min_count: int = 4,
) -> _Sig:
    """
    Count prior daily highs within `proximity` of current close.
    Dense cluster ≥ `min_count` = proven supply zone just overhead.
    """
    if len(df) < lookback + 1:
        return _Sig(False, 0.0)

    current = float(df["close"].iloc[-1])
    prior   = df.iloc[-(lookback + 1):-1]

    nearby = int(((prior["high"] - current).abs() / current <= proximity).sum())

    return _Sig(
        triggered=nearby >= min_count,
        value=float(nearby),
        description=(
            f"Resistance zone: {nearby} prior highs within {proximity:.1%} "
            f"of ${current:.2f} (need <{min_count})"
        ),
    )


def _check_rsi_divergence(
    df: pd.DataFrame,
    compare_bars: int = 10,
    rsi_period: int = 14,
) -> _Sig:
    """
    Bearish divergence: price made a higher high vs `compare_bars` ago
    but RSI made a *lower* high — momentum is fading behind the move.
    """
    if len(df) < rsi_period + compare_bars + 2:
        return _Sig(False, 0.0)

    close = df["close"]
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(rsi_period, min_periods=rsi_period // 2).mean()
    loss  = (-delta.clip(upper=0)).rolling(rsi_period, min_periods=rsi_period // 2).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    price_now, rsi_now   = float(close.iloc[-1]),            float(rsi.iloc[-1])
    price_ref, rsi_ref   = float(close.iloc[-(compare_bars + 1)]), float(rsi.iloc[-(compare_bars + 1)])

    if np.isnan(rsi_now) or np.isnan(rsi_ref):
        return _Sig(False, 0.0)

    price_up   = price_now > price_ref
    rsi_down   = rsi_now   < rsi_ref
    divergence = price_up and rsi_down
    rsi_delta  = rsi_now - rsi_ref

    return _Sig(
        triggered=divergence,
        value=rsi_delta,
        description=(
            f"RSI divergence: price ↑ {((price_now/price_ref-1)*100):+.1f}% "
            f"but RSI {rsi_now:.1f} vs {rsi_ref:.1f} ({rsi_delta:+.1f}) — momentum fading"
        ),
    )


def _check_narrow_bar(df: pd.DataFrame) -> _Sig:
    """
    A powerful breakout bar should have an above-average range.
    Today's range < 70% of ATR(14) = low-conviction breakout thrust.
    """
    if len(df) < 16:
        return _Sig(False, 0.0)

    bar   = df.iloc[-1]
    today = float(bar["high"] - bar["low"])

    prior    = df.iloc[-15:-1]
    h, l, c  = prior["high"], prior["low"], prior["close"]
    tr       = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr14    = float(tr.mean())

    if atr14 <= 0:
        return _Sig(False, 0.0)

    ratio = today / atr14
    return _Sig(
        triggered=ratio < 0.70,
        value=ratio,
        description=(
            f"Narrow breakout bar: range {today:.2f} = {ratio:.0%} of ATR14 {atr14:.2f} "
            f"(need >70% for conviction)"
        ),
    )


def _check_extension(
    df: pd.DataFrame,
    window: int = 20,
    max_extension: float | None = None,
) -> _Sig:
    """
    Warn when current close is already too far above the 20-day prior high (the breakout pivot).
    Risk/reward deteriorates significantly once a stock has run >BREAKOUT_MAX_EXTENSION_PCT
    above its base on entry day.
    """
    if len(df) < window + 1:
        return _Sig(False, 0.0)

    if max_extension is None:
        max_extension = config.BREAKOUT_MAX_EXTENSION_PCT

    close   = df["close"]
    current = float(close.iloc[-1])
    pivot   = float(close.iloc[-(window + 1):-1].max())

    if pivot <= 0:
        return _Sig(False, 0.0)

    ext_pct   = (current / pivot) - 1.0
    triggered = ext_pct > max_extension

    return _Sig(
        triggered=triggered,
        value=ext_pct,
        description=(
            f"Extended from pivot: {ext_pct:.1%} above ${pivot:.2f} "
            f"(threshold {max_extension:.1%}) — risk/reward deteriorated"
        ),
    )
