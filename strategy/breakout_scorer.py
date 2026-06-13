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


def _breadth_pts(breadth_pct: float) -> float:
    """Score based on % of S&P 500 above their 20-day MA. Shared by both scorers."""
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
        pts      = _max_pts()
        base     = min(sum(self._factor_pts(signals, f, pts) for f in pts), 100.0)
        bread    = self._breadth_pts(breadth_pct)
        bonus    = self._accum_bonus(signals)
        pen      = self._trap_penalty(signals)
        rsi_adj  = self._rsi_overbought_penalty(signals)
        gap_adj  = self._earnings_gap_penalty(signals)
        return round(max(0.0, min(base + bread + bonus - pen + rsi_adj + gap_adj, 100.0)), 1)

    def breakdown(self, signals: BreakoutSignals, breadth_pct: float = 0.5) -> dict[str, float]:
        """Per-factor base points + breadth + summary of bonus/penalty (for display)."""
        pts = _max_pts()
        bd  = {f: round(self._factor_pts(signals, f, pts), 1) for f in pts}
        bd["market_breadth"]       = round(self._breadth_pts(breadth_pct), 1)
        bd["accum_bonus"]          = round(self._accum_bonus(signals), 1)
        bd["trap_penalty"]         = round(-self._trap_penalty(signals), 1)
        bd["rsi_overbought"]       = round(self._rsi_overbought_penalty(signals), 1)
        bd["earnings_gap_penalty"] = round(self._earnings_gap_penalty(signals), 1)
        return bd

    # ── Base factor scoring ───────────────────────────────────────────────────

    def _factor_pts(self, signals: BreakoutSignals, factor: str, pts: dict) -> float:
        sig = getattr(signals, factor, None)
        if sig is None or not sig.triggered:
            return 0.0

        max_pts = pts[factor]

        # ── Graded signals (partial credit based on magnitude) ────────────────

        if factor == "volume_surge":
            ratio    = max(sig.value, config.BREAKOUT_VOLUME_SURGE_MULT)
            gap_pct  = abs(signals.gap_pct * 100)
            if gap_pct >= 3.0:
                # High volume on a large gap means institutions selling into
                # the news spike, not accumulating into a real breakout.
                return -8.0
            return min((ratio - 1.0) / 2.0 * max_pts, max_pts)

        if factor == "breakout_20d":
            pct = max(sig.value, 0.0)
            return min(max_pts * 0.5 + pct / 3.0 * max_pts * 0.5, max_pts)

        if factor == "relative_strength":
            rs = sig.value
            if rs > 25.0:
                # Extreme RS almost always means a post-earnings spike, not a
                # real technical setup — penalise rather than reward.
                return -10.0
            return min(max(rs, 0.0) / 10.0 * max_pts, max_pts)

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
        return _breadth_pts(breadth_pct)

    # ── Adjustments ───────────────────────────────────────────────────────────

    def _accum_bonus(self, signals: BreakoutSignals) -> float:
        if signals.accumulation is None:
            return 0.0
        return signals.accumulation.composite_score * config.SCORE_ACCUM_MAX_BONUS

    def _trap_penalty(self, signals: BreakoutSignals) -> float:
        if signals.bull_trap is None:
            return 0.0
        return signals.bull_trap.trap_score * (config.SCORE_TRAP_MAX_PENALTY / 100.0)

    def _rsi_overbought_penalty(self, signals: BreakoutSignals) -> float:
        """Penalise extended RSI — stocks above 75 are already overbought.

        The RSI zone signal gives 0 pts for RSI outside 50–65, but does not
        subtract anything. Candidates with RSI 75–92 were consistently our
        worst performers (-5% to -26% within days of the signal).

        Returns a negative number (penalty) or 0.
        """
        rsi = signals.rsi_zone.value
        if rsi <= 75:
            return 0.0
        if rsi <= 80:
            return -10.0   # moderately extended
        return -20.0       # severely overbought — near-certain short-term reversal

    def _earnings_gap_penalty(self, signals: BreakoutSignals) -> float:
        """Penalise large intraday gaps combined with overbought / extreme RS.

        A gap-up on earnings is NOT a technical breakout — it is a news event.
        Price typically fades within days as the initial buyers take profits.
        The bigger the gap AND the more extreme the RS, the harsher the penalty.

        Returns a negative number (penalty) or 0.
        """
        gap = abs(signals.gap_pct * 100)   # convert to %
        rs  = signals.relative_strength.value
        rsi = signals.rsi_zone.value

        if gap >= 5.0:
            # Any gap ≥5% is almost certainly earnings — heavy penalty regardless of RS
            return -25.0
        if gap >= 3.0 and rsi > 75:
            # Moderate gap on already-overbought stock = classic post-earnings fade
            return -30.0
        if gap >= 2.0 and rs > 30.0:
            # News-driven RS spike masquerading as momentum breakout
            return -20.0
        return 0.0


def _setup_max_pts() -> dict[str, float]:
    """Weight table for SETUP candidates — replaces breakout_20d with proximity_20d_high."""
    return {
        "vcp":                config.SCORE_VCP,
        "consolidation":      config.SCORE_CONSOLIDATION,
        "higher_lows":        config.SCORE_HIGHER_LOWS,
        "high_52w_proximity": config.SCORE_52W_HIGH_PROXIMITY,
        "earnings_proximity": config.SCORE_EARNINGS_PROXIMITY,
        "proximity_20d_high": config.SCORE_PROXIMITY_20D,
        "rsi_zone":           config.SCORE_RSI_ZONE,
        "relative_strength":  config.SCORE_RELATIVE_STRENGTH,
        # volume_surge and breakout_20d intentionally excluded
    }


class SetupScorer:
    """
    Scores pre-breakout SETUP candidates.
    Uses setup-quality signals only — no volume surge, no breakout_20d, no bull-trap penalty.
    proximity_20d_high replaces breakout_20d in the weight table (graded by closeness to level).
    """

    def score(self, signals: BreakoutSignals, breadth_pct: float = 0.5) -> float:
        pts  = _setup_max_pts()
        base = min(sum(self._factor_pts(signals, f, pts) for f in pts), 100.0)
        return round(max(0.0, min(base + _breadth_pts(breadth_pct) + self._accum_bonus(signals), 100.0)), 1)

    def breakdown(self, signals: BreakoutSignals, breadth_pct: float = 0.5) -> dict[str, float]:
        pts = _setup_max_pts()
        bd  = {f: round(self._factor_pts(signals, f, pts), 1) for f in pts}
        bd["market_breadth"] = round(_breadth_pts(breadth_pct), 1)
        bd["accum_bonus"]    = round(self._accum_bonus(signals), 1)
        return bd

    def _factor_pts(self, signals: BreakoutSignals, factor: str, pts: dict) -> float:
        sig = getattr(signals, factor, None)
        if sig is None or not sig.triggered:
            return 0.0
        max_pts = pts[factor]

        if factor == "proximity_20d_high":
            # Graded: at threshold (-5%) → 0 pts, at 0% (touching level) → full pts
            pct           = sig.value  # negative, e.g. -3.2
            threshold_pct = -config.SETUP_PROXIMITY_PCT * 100  # e.g. -5.0
            if threshold_pct >= 0:
                return 0.0
            fraction = 1.0 - abs(pct) / abs(threshold_pct)
            return round(max(0.0, min(max_pts * fraction, max_pts)), 1)

        if factor == "relative_strength":
            rs = max(sig.value, 0.0)
            return min(rs / 10.0 * max_pts, max_pts)

        if factor == "high_52w_proximity":
            pct = sig.value
            if pct >= -3.0:
                return max_pts
            return max(0.0, max_pts * (pct + 10.0) / 7.0)

        if factor == "vcp":
            n = sig.value
            return max_pts if n >= 2 else max_pts * 0.55

        return max_pts  # binary signals (consolidation, higher_lows, earnings_proximity, rsi_zone)

    def _accum_bonus(self, signals: BreakoutSignals) -> float:
        if signals.accumulation is None:
            return 0.0
        return signals.accumulation.composite_score * config.SCORE_ACCUM_MAX_BONUS
