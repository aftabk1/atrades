"""
Weighted points scorer for breakout candidates.

Scoring pipeline:
  1. BASE SCORE  — 9-factor system; weights read from config, sum to 100
  2. BREADTH     — market-wide participation score (passed in, not per-symbol)
  3. ACCUM BONUS — institutional accumulation composite adds up to SCORE_ACCUM_MAX_BONUS pts
  4. TRAP PENALTY— bull-trap score subtracts up to SCORE_TRAP_MAX_PENALTY pts
  5. CLAMP       — final score is clamped to [0, 100]

Removed (replaced):
  - breakout_50d   → high_52w_proximity (overhead supply vs redundant confirmation)
  - atr_expansion  → vcp (structure vs lagging volatility measure)
  - regime multiplier → market_breadth (participation-based, passed from scanner)
"""

from __future__ import annotations

import config
from .breakout_signals import BreakoutSignals


def _max_pts() -> dict[str, float]:
    """Build the per-factor weight dict from live config values."""
    return {
        # ── Predictive / setup-quality signals ───────────────────────────────
        "vcp":                config.SCORE_VCP,
        "consolidation":      config.SCORE_CONSOLIDATION,
        "higher_lows":        config.SCORE_HIGHER_LOWS,
        "high_52w_proximity": config.SCORE_52W_HIGH_PROXIMITY,
        "earnings_proximity": config.SCORE_EARNINGS_PROXIMITY,
        # ── Confirmation signals ──────────────────────────────────────────────
        "volume_surge":       config.SCORE_VOLUME_SURGE,
        "breakout_20d":       config.SCORE_BREAKOUT_20D,
        "rsi_zone":           config.SCORE_RSI_ZONE,
        "relative_strength":  config.SCORE_RELATIVE_STRENGTH,
    }


class BreakoutScorer:

    def score(self, signals: BreakoutSignals, breadth_pct: float = 0.5) -> float:
        """Return final confidence score in [0, 100].

        breadth_pct — fraction of S&P 500 stocks above their 20-day MA,
                      computed once per scan run and passed in from scanner.py.
        """
        pts   = _max_pts()
        base  = min(sum(self._factor_pts(signals, f, pts) for f in pts), 100.0)
        bread = self._breadth_pts(breadth_pct)
        bonus = self._accum_bonus(signals)
        pen   = self._trap_penalty(signals)
        return round(max(0.0, min(base + bread + bonus - pen, 100.0)), 1)

    def breakdown(self, signals: BreakoutSignals, breadth_pct: float = 0.5) -> dict[str, float]:
        """Per-factor base points + breadth + summary of bonus/penalty (for display)."""
        pts = _max_pts()
        bd  = {f: round(self._factor_pts(signals, f, pts), 1) for f in pts}
        bd["market_breadth"] = round(self._breadth_pts(breadth_pct), 1)
        bd["accum_bonus"]    = round(self._accum_bonus(signals), 1)
        bd["trap_penalty"]   = round(-self._trap_penalty(signals), 1)
        return bd

    # ── Base factor scoring ───────────────────────────────────────────────────

    def _factor_pts(self, signals: BreakoutSignals, factor: str, pts: dict) -> float:
        sig = getattr(signals, factor, None)
        if sig is None or not sig.triggered:
            return 0.0

        max_pts = pts[factor]

        # ── Graded signals (partial credit based on magnitude) ────────────────

        if factor == "volume_surge":
            ratio = max(sig.value, config.BREAKOUT_VOLUME_SURGE_MULT)
            return min((ratio - 1.0) / 2.0 * max_pts, max_pts)

        if factor == "breakout_20d":
            pct = max(sig.value, 0.0)
            return min(max_pts * 0.5 + pct / 3.0 * max_pts * 0.5, max_pts)

        if factor == "relative_strength":
            rs = max(sig.value, 0.0)
            return min(rs / 10.0 * max_pts, max_pts)

        if factor == "high_52w_proximity":
            pct = sig.value   # 0 = at 52w high, negative = below
            if pct >= -3.0:
                return max_pts                          # at highs — full pts
            # Linear scale: -3% → full, -10% → 0
            return max(0.0, max_pts * (pct + 10.0) / 7.0)

        if factor == "vcp":
            n = sig.value   # number of contracting swing pairs
            if n >= 2:
                return max_pts          # 2+ contractions — fully compressed
            return max_pts * 0.55       # 1 contraction — early compression

        # ── Binary signals (full points when triggered) ───────────────────────
        return max_pts

    # ── Market breadth ────────────────────────────────────────────────────────

    def _breadth_pts(self, breadth_pct: float) -> float:
        """
        Score based on % of S&P 500 above their 20-day MA.
        Healthy market (>=70%) = full SCORE_MARKET_BREADTH pts.
        Narrow/weak market (<40%) = 0 pts.
        """
        max_pts = config.SCORE_MARKET_BREADTH
        if breadth_pct >= 0.70:
            return max_pts
        if breadth_pct >= 0.60:
            return max_pts * 0.75
        if breadth_pct >= 0.50:
            return max_pts * 0.50
        if breadth_pct >= 0.40:
            return max_pts * 0.25
        return 0.0

    # ── Adjustments ───────────────────────────────────────────────────────────

    def _accum_bonus(self, signals: BreakoutSignals) -> float:
        if signals.accumulation is None:
            return 0.0
        return signals.accumulation.composite_score * config.SCORE_ACCUM_MAX_BONUS

    def _trap_penalty(self, signals: BreakoutSignals) -> float:
        if signals.bull_trap is None:
            return 0.0
        return signals.bull_trap.trap_score * (config.SCORE_TRAP_MAX_PENALTY / 100.0)
