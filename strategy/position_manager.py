"""
Position Management Engine (PME).

Evaluates each open trade end-of-day and produces an action recommendation:
  ADD         — re-enter up to PME_ADD_SIZE_PCT more; only at ≥PME_ADD_SCORE_THRESHOLD
  HOLD        — keep full position, do nothing
  TRIM_LIGHT  — sell PME_TRIM_LIGHT_PCT (25%) of remaining shares
  TRIM_HEAVY  — sell PME_TRIM_HEAVY_PCT (60%) of remaining shares
  EXIT        — close full position immediately

Decision logic (applied in priority order):
  1. Trap override: if bull_trap fires → EXIT regardless of score
  2. R-multiple floor: score-based action is degraded by one tier when R < PME_R_TRIM_FLOOR
     and enforced to TRIM_HEAVY when R ≥ PME_R_TRIM_ENFORCE but score < HOLD floor
  3. RS filter: ADD requires rs_vs_spy ≥ PME_RS_ADD_MIN_PCT; if rs_vs_spy < PME_RS_DOWNGRADE_BELOW_PCT
     any ADD→HOLD and HOLD→TRIM_LIGHT
  4. Score tier: ADD ≥ PME_ADD_SCORE_THRESHOLD, HOLD ≥ PME_HOLD_SCORE_MIN,
     TRIM_LIGHT ≥ PME_TRIM_LIGHT_SCORE_MIN, TRIM_HEAVY ≥ PME_TRIM_HEAVY_SCORE_MIN, else EXIT
  5. Follow-through guard: ADD blocked if this position was already added within
     PME_FOLLOWTHROUGH_DAYS days (prevents double-add on noisy signals)
  6. High-volume selloff: if today's down-close with volume > PME_VOLUME_SELLOFF_MULT × avg → EXIT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from loguru import logger

import config
from data.market_data import MarketDataClient
from data.store import (
    get_open_trades,
    get_position_evaluations,
    save_position_evaluation,
    update_highest_price,
)
from strategy.breakout_signals import BreakoutSignals, detect_all
from strategy.breakout_scorer import BreakoutScorer
from strategy.bull_trap import detect_bull_trap


# ── Action constants ──────────────────────────────────────────────────────────

ADD         = "ADD"
HOLD        = "HOLD"
TRIM_LIGHT  = "TRIM_LIGHT"
TRIM_HEAVY  = "TRIM_HEAVY"
EXIT        = "EXIT"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PositionEvaluation:
    symbol:          str
    buy_order_id:    str
    action:          str        # ADD / HOLD / TRIM_LIGHT / TRIM_HEAVY / EXIT
    score:           float
    r_multiple:      float
    rs_vs_spy:       float
    trap_triggered:  bool
    reason:          str
    signals:         Optional[BreakoutSignals] = field(default=None, repr=False)
    # Sizing helpers filled in by executor
    current_shares:  int = 0
    fill_price:      float = 0.0
    current_price:   float = 0.0


# ── Engine ────────────────────────────────────────────────────────────────────

class PositionManager:
    def __init__(self) -> None:
        self._market = MarketDataClient()
        self._scorer = BreakoutScorer()

    def evaluate_all(self, spy_df: pd.DataFrame | None = None) -> list[PositionEvaluation]:
        """
        Evaluate every open/partial_exit trade and return a list of evaluations.
        Also persists each evaluation to the DB and updates highest_price_since_entry.
        """
        trades = get_open_trades()
        if not trades:
            return []

        symbols = list({t["symbol"] for t in trades if t.get("symbol")})
        logger.info(f"PME: evaluating {len(symbols)} open positions")

        if spy_df is None:
            try:
                spy_df = self._market.get_spy_data(days=120)
            except Exception as exc:
                logger.warning(f"PME: could not fetch SPY data: {exc}")
                spy_df = None

        bars = self._market.get_daily_bars(symbols, days=120)

        # Compute breadth from available bars (open positions only — proxy, not full universe)
        breadth_count = sum(
            1 for df in bars.values()
            if len(df) >= 22 and df["close"].iloc[-1] > df["close"].iloc[-22:-1].mean()
        )
        breadth_pct = breadth_count / max(len(bars), 1) if bars else 0.5

        today = date.today().isoformat()
        results: list[PositionEvaluation] = []
        for trade in trades:
            sym   = trade.get("symbol", "")
            boid  = trade.get("buy_order_id", "")

            # Skip trades opened today — too early to evaluate
            if trade.get("date", "") == today:
                logger.info(f"PME: {sym} opened today — skipping evaluation")
                continue

            df    = bars.get(sym)
            if df is None or df.empty:
                logger.warning(f"PME: no market data for {sym} — skipping")
                continue

            try:
                ev = self._evaluate_one(trade, df, spy_df, breadth_pct)
            except Exception as exc:
                logger.error(f"PME: error evaluating {sym}: {exc}")
                continue

            # Ratchet highest price
            current_px = ev.current_price
            if current_px > 0:
                try:
                    update_highest_price(boid, current_px)
                except Exception:
                    pass

            # Persist evaluation
            try:
                save_position_evaluation({
                    "symbol":         sym,
                    "buy_order_id":   boid,
                    "score":          ev.score,
                    "action":         ev.action,
                    "r_multiple":     ev.r_multiple,
                    "rs_vs_spy":      ev.rs_vs_spy,
                    "trap_triggered": ev.trap_triggered,
                    "reason":         ev.reason,
                    "executed":       False,
                })
            except Exception:
                pass

            results.append(ev)
            logger.info(
                f"PME {sym}: {ev.action} | score={ev.score:.0f} "
                f"R={ev.r_multiple:.2f} rs={ev.rs_vs_spy:+.1f}% — {ev.reason}"
            )

        return results

    # ── Per-trade evaluation ──────────────────────────────────────────────────

    def _evaluate_one(
        self,
        trade: dict,
        df: pd.DataFrame,
        spy_df: pd.DataFrame | None,
        breadth_pct: float = 0.5,
    ) -> PositionEvaluation:
        sym      = trade["symbol"]
        boid     = trade.get("buy_order_id", "")
        fill_px  = float(trade.get("fill_price") or trade.get("entry") or 0)
        stop_loss = float(trade.get("stop_loss") or 0)
        shares   = int(trade.get("shares") or 0)

        current_px = float(df["close"].iloc[-1])

        # ── R-multiple ───────────────────────────────────────────────────────
        risk_per_share = fill_px - stop_loss
        r_multiple = 0.0
        if risk_per_share > 0:
            r_multiple = round((current_px - fill_px) / risk_per_share, 2)

        # ── Re-score via signals pipeline ────────────────────────────────────
        signals = detect_all(
            sym, df, spy_df,
            require_breakout=False,
            fast=False,
        )

        score    = 0.0
        rs_vs_spy = 0.0
        trap_triggered = False

        if signals is not None:
            score     = self._scorer.score(signals, breadth_pct)
            rs_vs_spy = round(signals.relative_strength.value, 2)

            # Refresh bull trap directly on the current df
            try:
                signals.bull_trap = detect_bull_trap(df)
            except Exception:
                pass
            trap_triggered = bool(signals.bull_trap and signals.bull_trap.is_trap)
        else:
            # detect_all returned None (base filters failed or insufficient data)
            # treat as weak signal
            score = 0.0

        # ── High-volume selloff check ────────────────────────────────────────
        volume_selloff = False
        if len(df) >= 2:
            today_close = float(df["close"].iloc[-1])
            today_open  = float(df["open"].iloc[-1])
            today_vol   = float(df["volume"].iloc[-1])
            avg_vol     = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else today_vol
            down_day    = today_close < today_open
            vol_spike   = avg_vol > 0 and today_vol > avg_vol * config.PME_VOLUME_SELLOFF_MULT
            volume_selloff = down_day and vol_spike

        # ── Follow-through guard (for ADD) ───────────────────────────────────
        recent_adds = _count_recent_actions(boid, ADD, days=config.PME_FOLLOWTHROUGH_DAYS)
        already_added = recent_adds > 0

        # ── Decision tree ────────────────────────────────────────────────────
        action, reason = self._decide(
            score, r_multiple, rs_vs_spy, trap_triggered, volume_selloff, already_added
        )

        return PositionEvaluation(
            symbol=sym,
            buy_order_id=boid,
            action=action,
            score=score,
            r_multiple=r_multiple,
            rs_vs_spy=rs_vs_spy,
            trap_triggered=trap_triggered,
            reason=reason,
            signals=signals,
            current_shares=shares,
            fill_price=fill_px,
            current_price=current_px,
        )

    # ── Decision logic ────────────────────────────────────────────────────────

    def _decide(
        self,
        score: float,
        r: float,
        rs: float,
        trap: bool,
        volume_selloff: bool,
        already_added: bool,
    ) -> tuple[str, str]:
        # 1. Hard overrides
        if trap:
            return EXIT, "Bull trap detected — exiting position"
        if volume_selloff:
            return EXIT, f"High-volume down-day (>{config.PME_VOLUME_SELLOFF_MULT:.0f}x avg) — distribution signal"

        # 2. Score-based tier
        if score >= config.PME_ADD_SCORE_THRESHOLD:
            action = ADD
        elif score >= config.PME_HOLD_SCORE_MIN:
            action = HOLD
        elif score >= config.PME_TRIM_LIGHT_SCORE_MIN:
            action = TRIM_LIGHT
        elif score >= config.PME_TRIM_HEAVY_SCORE_MIN:
            action = TRIM_HEAVY
        else:
            action = EXIT

        reason_parts = [f"score={score:.0f}"]

        # 3. R-multiple adjustments
        if r < config.PME_R_TRIM_FLOOR and action in (HOLD, ADD):
            action = TRIM_LIGHT
            reason_parts.append(f"R={r:.2f} below floor {config.PME_R_TRIM_FLOOR:.1f} — downgraded to TRIM_LIGHT")
        elif r >= config.PME_R_TRIM_ENFORCE and action in (TRIM_HEAVY, EXIT) and score >= config.PME_HOLD_SCORE_MIN:
            action = TRIM_HEAVY
            reason_parts.append(f"R={r:.2f} — locking in profit with TRIM_HEAVY")

        # 4. RS filter
        if action == ADD:
            if rs < config.PME_RS_ADD_MIN_PCT:
                action = HOLD
                reason_parts.append(f"RS={rs:+.1f}% below ADD minimum {config.PME_RS_ADD_MIN_PCT:.0f}% — hold only")
            elif already_added:
                action = HOLD
                reason_parts.append(f"Already added within {config.PME_FOLLOWTHROUGH_DAYS}d — skipping re-add")
        elif action == HOLD and rs < config.PME_RS_DOWNGRADE_BELOW_PCT:
            action = TRIM_LIGHT
            reason_parts.append(f"RS={rs:+.1f}% below {config.PME_RS_DOWNGRADE_BELOW_PCT:.0f}% — trimming weak relative strength")

        if action == ADD and not already_added:
            reason_parts.append(f"RS={rs:+.1f}% — adding to winner")
        elif action == HOLD:
            reason_parts.append("holding")
        elif action in (TRIM_LIGHT, TRIM_HEAVY):
            pct = config.PME_TRIM_LIGHT_PCT if action == TRIM_LIGHT else config.PME_TRIM_HEAVY_PCT
            reason_parts.append(f"trimming {pct*100:.0f}%")

        return action, " | ".join(reason_parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_recent_actions(buy_order_id: str, action: str, days: int) -> int:
    """Count how many times `action` was recorded for this trade in the last N days."""
    try:
        evals  = get_position_evaluations(buy_order_id=buy_order_id)
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return sum(
            1 for e in evals
            if e.get("action") == action and e.get("date", "") >= cutoff
        )
    except Exception:
        return 0
