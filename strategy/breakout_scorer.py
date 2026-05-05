"""
Weighted points scorer for breakout candidates.

Scoring pipeline:
  1. BASE SCORE  — 9-factor system; weights read from config, sum to 100
  2. ACCUM BONUS — institutional accumulation composite adds up to SCORE_ACCUM_MAX_BONUS pts
  3. TRAP PENALTY— bull-trap score subtracts up to SCORE_TRAP_MAX_PENALTY pts
  4. CLAMP       — final score is clamped to [0, 100]
"""

from __future__ import annotations

import config
from .breakout_signals import BreakoutSignals


def _max_pts() -> dict[str, float]:
    """Build the per-factor weight dict from live config values."""
    return {
        "volume_surge":       config.SCORE_VOLUME_SURGE,
        "breakout_20d":       config.SCORE_BREAKOUT_20D,
        "relative_strength":  config.SCORE_RELATIVE_STRENGTH,
        "rsi_zone":           config.SCORE_RSI_ZONE,
        "breakout_50d":       config.SCORE_BREAKOUT_50D,
        "atr_expansion":      config.SCORE_ATR_EXPANSION,
        "consolidation":      config.SCORE_CONSOLIDATION,
        "higher_lows":        config.SCORE_HIGHER_LOWS,
        "earnings_proximity": config.SCORE_EARNINGS_PROXIMITY,
    }


class BreakoutScorer:

    def score(self, signals: BreakoutSignals) -> float:
        """Return final confidence score in [0, 100]."""
        pts = _max_pts()
        base    = min(sum(self._factor_pts(signals, f, pts) for f in pts), 100.0)
        bonus   = self._accum_bonus(signals)
        penalty = self._trap_penalty(signals)
        return round(max(0.0, min(base + bonus - penalty, 100.0)), 1)

    def breakdown(self, signals: BreakoutSignals) -> dict[str, float]:
        """Per-factor base points + summary of bonus/penalty (for display)."""
        pts = _max_pts()
        bd = {f: round(self._factor_pts(signals, f, pts), 1) for f in pts}
        bd["accum_bonus"]  = round(self._accum_bonus(signals), 1)
        bd["trap_penalty"] = round(-self._trap_penalty(signals), 1)
        return bd

    # ── Base factor scoring ───────────────────────────────────────────────────

    def _factor_pts(self, signals: BreakoutSignals, factor: str, pts: dict) -> float:
        sig = getattr(signals, factor, None)
        if sig is None or not sig.triggered:
            return 0.0

        max_pts = pts[factor]

        if factor == "volume_surge":
            ratio = max(sig.value, config.BREAKOUT_VOLUME_SURGE_MULT)
            return min((ratio - 1.0) / 2.0 * max_pts, max_pts)

        if factor == "breakout_20d":
            pct = max(sig.value, 0.0)
            return min(max_pts * 0.5 + pct / 3.0 * max_pts * 0.5, max_pts)

        if factor == "relative_strength":
            rs = max(sig.value, 0.0)
            return min(rs / 10.0 * max_pts, max_pts)

        return max_pts

    # ── Adjustments ───────────────────────────────────────────────────────────

    def _accum_bonus(self, signals: BreakoutSignals) -> float:
        if signals.accumulation is None:
            return 0.0
        return signals.accumulation.composite_score * config.SCORE_ACCUM_MAX_BONUS

    def _trap_penalty(self, signals: BreakoutSignals) -> float:
        if signals.bull_trap is None:
            return 0.0
        return signals.bull_trap.trap_score * (config.SCORE_TRAP_MAX_PENALTY / 100.0)
