"""
Institutional accumulation detector.

Identifies signatures of sustained professional buying:
  • OBV trend     — On-Balance Volume MA crossover (smart money footprint in volume)
  • Chaikin MF    — Net money flow pressure over 20 days (accumulation vs distribution)
  • Up/Down vol   — More share volume traded on up-days than down-days
  • Inst. days    — Count of high-volume up-close bars (Weinstein accumulation pattern)

All checks look at bars *before* the current breakout bar to avoid
using today's move as evidence of accumulation.
Fully self-contained — no imports from other strategy modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np
import pandas as pd


class _Sig(NamedTuple):
    triggered: bool
    value: float
    description: str = ""


@dataclass
class AccumulationSignals:
    obv_trend:          _Sig = _Sig(False, 0.0)
    chaikin_mf:         _Sig = _Sig(False, 0.0)
    up_down_vol_ratio:  _Sig = _Sig(False, 0.0)
    institutional_days: _Sig = _Sig(False, 0.0)
    composite_score: float = 0.0   # fraction 0–1 (how many of the 4 signals triggered)


def detect_accumulation(df: pd.DataFrame, lookback: int = 20) -> AccumulationSignals:
    """
    Run all accumulation checks on the bars *preceding* the current bar.
    Returns AccumulationSignals with a composite_score in [0, 1].
    """
    if df is None or len(df) < lookback + 2:
        return AccumulationSignals()

    # Always exclude today's bar (index -1) so we measure pre-breakout behaviour
    window = df.iloc[-(lookback + 1):-1]

    obv   = _obv_trend(window)
    cmf   = _chaikin_money_flow(window)
    udv   = _up_down_volume(window)
    idays = _institutional_days(df, lookback)  # uses full df for avg vol baseline

    n_triggered  = sum(s.triggered for s in (obv, cmf, udv, idays))
    composite    = n_triggered / 4.0

    return AccumulationSignals(
        obv_trend=obv,
        chaikin_mf=cmf,
        up_down_vol_ratio=udv,
        institutional_days=idays,
        composite_score=composite,
    )


# ── Individual detectors ──────────────────────────────────────────────────────

def _obv_trend(df: pd.DataFrame) -> _Sig:
    """OBV 5-day SMA crosses above OBV 20-day SMA — short-term accumulation momentum."""
    if len(df) < 20:
        return _Sig(False, 0.0)

    close  = df["close"]
    volume = df["volume"]

    direction = np.sign(close.diff().fillna(0))
    obv       = (direction * volume).cumsum()

    fast = float(obv.rolling(5,  min_periods=1).mean().iloc[-1])
    slow = float(obv.rolling(20, min_periods=1).mean().iloc[-1])

    # Normalise the difference relative to OBV's own standard deviation
    spread = (fast - slow) / (float(obv.std()) or 1.0)

    return _Sig(
        triggered=fast > slow,
        value=spread,
        description=(
            f"OBV trend: MA5 {'>' if fast > slow else '<'} MA20 — "
            f"spread={spread:+.2f}σ ({'accumulating' if fast > slow else 'distributing'})"
        ),
    )


def _chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> _Sig:
    """
    CMF = Σ(money-flow-volume) / Σ(volume).
    Money-flow multiplier = ((C−L) − (H−C)) / (H−L).
    CMF > +0.05 indicates sustained net buying pressure.
    """
    if len(df) < max(period // 2, 5):
        return _Sig(False, 0.0)

    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    hl    = (h - l).replace(0, np.nan)
    mfm   = ((c - l) - (h - c)) / hl          # money-flow multiplier in [-1, +1]
    mfv   = mfm * v                             # money-flow volume

    min_p = max(period // 2, 5)
    cmf_num = mfv.rolling(period, min_periods=min_p).sum().iloc[-1]
    cmf_den = v.rolling(period, min_periods=min_p).sum().iloc[-1]

    if cmf_den == 0 or np.isnan(cmf_num) or np.isnan(cmf_den):
        return _Sig(False, 0.0)

    cmf = float(cmf_num / cmf_den)

    label = "accumulation" if cmf > 0.05 else ("distribution" if cmf < -0.05 else "neutral")
    return _Sig(
        triggered=cmf > 0.05,
        value=cmf,
        description=f"Chaikin MF({period}): {cmf:+.3f} ({label})",
    )


def _up_down_volume(df: pd.DataFrame, lookback: int = 10) -> _Sig:
    """
    Ratio of volume on up-close days vs down-close days over `lookback` bars.
    Ratio ≥ 1.5 signals that more capital is flowing in on strength than weakness.
    """
    if len(df) < lookback:
        return _Sig(False, 0.0)

    recent   = df.iloc[-lookback:]
    is_up    = recent["close"] >= recent["open"]

    up_vol   = float(recent.loc[is_up,  "volume"].sum())
    down_vol = float(recent.loc[~is_up, "volume"].sum())

    if down_vol == 0:
        return _Sig(True, 99.0, f"Up/Down Vol ({lookback}d): all up-day volume — strong demand")

    ratio = up_vol / down_vol
    return _Sig(
        triggered=ratio >= 1.5,
        value=ratio,
        description=f"Up/Down Vol ({lookback}d): {ratio:.2f}x ({'accumulation ↑' if ratio >= 1.5 else 'neutral'})",
    )


def _institutional_days(df: pd.DataFrame, lookback: int = 20) -> _Sig:
    """
    'Institutional buying day': an up-close bar whose volume exceeds 1.5× the
    20-day average. ≥ 3 such days in `lookback` bars = repeated block buying.
    """
    if len(df) < lookback + 1:
        return _Sig(False, 0.0)

    avg_vol = float(df["volume"].mean())
    recent  = df.iloc[-(lookback + 1):-1]   # exclude today

    inst_mask = (recent["close"] > recent["open"]) & (recent["volume"] > 1.5 * avg_vol)
    count     = int(inst_mask.sum())

    return _Sig(
        triggered=count >= 3,
        value=float(count),
        description=f"Institutional buying days: {count}/{lookback} (need ≥3)",
    )
