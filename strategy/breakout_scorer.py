"""
Weighted points scorer for breakout candidates.

Scoring pipeline:
  1. BASE SCORE  — existing 9-factor system (0–125 pts, normalised to 0–100)
  2. ACCUM BONUS — institutional accumulation composite adds up to +15 pts
  3. TRAP PENALTY— bull-trap score subtracts up to −40 pts from the adjusted score
  4. CLAMP       — final score is clamped to [0, 100]

A candidate that looks great on price/volume action but is simultaneously
flagged as a likely trap will have its confidence score materially reduced,
pushing it below the minimum-score threshold that gates order placement.
"""

from __future__ import annotations

from .breakout_signals import BreakoutSignals

# ── Base factor allocations (max raw = 125, normalised to 100) ────────────────
_MAX_PTS: dict[str, float] = {
    "volume_surge":       25.0,   # strongest confirmation signal
    "breakout_20d":       20.0,   # mandatory anchor
    "relative_strength":  15.0,   # outperforming the market
    "rsi_zone":           15.0,   # momentum sweet spot
    "breakout_50d":       15.0,   # multi-month high breakout
    "atr_expansion":      10.0,   # volatility expanding into the move
    "consolidation":      10.0,   # tight base preceding breakout
    "higher_lows":        10.0,   # demand building under price
    "earnings_proximity":  5.0,   # catalyst proximity bonus
}
_SCORE_MAX = sum(_MAX_PTS.values())   # 125

# ── Adjustment parameters ─────────────────────────────────────────────────────
_ACCUM_MAX_BONUS  = 15.0   # maximum bonus from accumulation signals
_TRAP_PENALTY_MUL =  0.40  # trap_score (0-100) × this → pts deducted


class BreakoutScorer:

    def score(self, signals: BreakoutSignals) -> float:
        """
        Return final confidence score in [0, 100].
        Applies accumulation bonus then trap penalty on top of the base score.
        """
        # 1. Base score (normalised)
        raw  = sum(self._factor_pts(signals, f) for f in _MAX_PTS)
        base = min(raw / _SCORE_MAX * 100.0, 100.0)

        # 2. Accumulation bonus (+0 to +15 pts)
        bonus = self._accum_bonus(signals)

        # 3. Trap penalty (0 to -40 pts)
        penalty = self._trap_penalty(signals)

        final = max(0.0, min(base + bonus - penalty, 100.0))
        return round(final, 1)

    def breakdown(self, signals: BreakoutSignals) -> dict[str, float]:
        """Per-factor base points + summary of bonus/penalty (for display)."""
        bd = {f: round(self._factor_pts(signals, f), 1) for f in _MAX_PTS}
        bd["accum_bonus"]  = round(self._accum_bonus(signals), 1)
        bd["trap_penalty"] = round(-self._trap_penalty(signals), 1)
        return bd

    # ── Base factor scoring ───────────────────────────────────────────────────

    def _factor_pts(self, signals: BreakoutSignals, factor: str) -> float:
        sig = getattr(signals, factor, None)
        if sig is None or not sig.triggered:
            return 0.0

        max_pts = _MAX_PTS[factor]

        if factor == "volume_surge":
            # 1.5x → 50% of max;  3.0x+ → 100%
            ratio = max(sig.value, config.BREAKOUT_VOLUME_SURGE_MULT)
            return min((ratio - 1.0) / 2.0 * max_pts, max_pts)

        if factor == "breakout_20d":
            # 0% above → 50% of max;  3%+ → 100%
            pct = max(sig.value, 0.0)
            return min(max_pts * 0.5 + pct / 3.0 * max_pts * 0.5, max_pts)

        if factor == "relative_strength":
            # RS +0% → 0 pts;  RS +10%+ → full points
            rs = max(sig.value, 0.0)
            return min(rs / 10.0 * max_pts, max_pts)

        # All other signals: binary (triggered = full allocation)
        return max_pts

    # ── Adjustments ───────────────────────────────────────────────────────────

    def _accum_bonus(self, signals: BreakoutSignals) -> float:
        """
        Accumulation bonus: composite_score (0–1) × max_bonus.
        A stock with all 4 accumulation signals firing earns the full +15 pts.
        """
        if signals.accumulation is None:
            return 0.0
        return signals.accumulation.composite_score * _ACCUM_MAX_BONUS

    def _trap_penalty(self, signals: BreakoutSignals) -> float:
        """
        Trap penalty: trap_score (0–100) × multiplier → pts subtracted.
        A stock with all 5 trap warnings (trap_score = 100) loses −40 pts,
        virtually guaranteeing it falls below any reasonable score floor.
        """
        if signals.bull_trap is None:
            return 0.0
        return signals.bull_trap.trap_score * _TRAP_PENALTY_MUL


import config  # noqa: E402
