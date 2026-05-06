"""
Position Management Engine — order executor.

Takes a PositionEvaluation and executes the recommended action via Alpaca:
  ADD        — market buy additional shares (sized to PME_ADD_SIZE_PCT of portfolio,
                capped at PME_ADD_MAX_MULTIPLIER × original position value,
                subject to existing open slots)
  TRIM_LIGHT — market sell PME_TRIM_LIGHT_PCT of current shares
  TRIM_HEAVY — market sell PME_TRIM_HEAVY_PCT of current shares
  EXIT       — cancel all open orders + market sell 100% of current shares
  HOLD       — no order; returns early with a logged note

All actions are no-ops when the market is closed.
"""

from __future__ import annotations

import math

from loguru import logger

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, QueryOrderStatus

import config
from broker.alpaca_client import AlpacaClient
from data.store import close_trade, get_position_evaluations, save_position_evaluation
from notifications.whatsapp import notify
from strategy.position_manager import (
    ADD, EXIT, HOLD, TRIM_HEAVY, TRIM_LIGHT,
    PositionEvaluation,
)

_MktReq = MarketOrderRequest
_Side   = OrderSide
_TIF    = TimeInForce


class PositionExecutor:
    def __init__(self, client: AlpacaClient) -> None:
        self._client = client

    def execute(self, ev: PositionEvaluation) -> dict:
        """
        Execute the recommended action for `ev`.
        Returns a result dict with keys: symbol, action, executed, qty, reason, error.
        """
        if ev.action == HOLD:
            logger.info(f"PME {ev.symbol}: HOLD — no order placed")
            return _result(ev, executed=False, qty=0)

        if not self._client.is_market_open():
            logger.info(f"PME {ev.symbol}: market closed — deferring {ev.action}")
            return _result(ev, executed=False, qty=0, error="market_closed")

        try:
            if ev.action == ADD:
                return self._do_add(ev)
            elif ev.action in (TRIM_LIGHT, TRIM_HEAVY):
                return self._do_trim(ev)
            elif ev.action == EXIT:
                return self._do_exit(ev)
        except Exception as exc:
            logger.error(f"PME executor error for {ev.symbol}: {exc}")
            return _result(ev, executed=False, qty=0, error=str(exc))

        return _result(ev, executed=False, qty=0)

    # ── ADD ───────────────────────────────────────────────────────────────────

    def _do_add(self, ev: PositionEvaluation) -> dict:
        tc = self._client.trading_client

        portfolio = self._client.get_portfolio_value()
        add_budget = portfolio * config.PME_ADD_SIZE_PCT

        # Cap: total invested (original + add) ≤ PME_ADD_MAX_MULTIPLIER × original
        orig_value = ev.fill_price * ev.current_shares
        max_add    = orig_value * (config.PME_ADD_MAX_MULTIPLIER - 1.0)
        add_budget = min(add_budget, max_add)

        qty = math.floor(add_budget / ev.current_price) if ev.current_price > 0 else 0
        if qty < 1:
            logger.warning(f"PME {ev.symbol}: ADD budget too small for 1 share — skipping")
            return _result(ev, executed=False, qty=0, error="budget_too_small")

        order = tc.submit_order(_MktReq(
            symbol=ev.symbol,
            qty=qty,
            side=_Side.BUY,
            time_in_force=_TIF.DAY,
        ))
        logger.info(f"PME ADD {ev.symbol} +{qty} sh | order_id={order.id}")
        notify(f"PME ADD: {ev.symbol} +{qty} sh @ ~${ev.current_price:.2f} | score={ev.score:.0f}")
        _mark_executed(ev)
        return _result(ev, executed=True, qty=qty, order_id=str(order.id))

    # ── TRIM ──────────────────────────────────────────────────────────────────

    def _do_trim(self, ev: PositionEvaluation) -> dict:
        tc   = self._client.trading_client
        pct  = config.PME_TRIM_LIGHT_PCT if ev.action == TRIM_LIGHT else config.PME_TRIM_HEAVY_PCT

        # Get live qty from Alpaca (may differ from DB if partial fills occurred)
        live_qty = _live_qty(self._client, ev.symbol)
        qty = max(1, math.floor(live_qty * pct))

        if qty < 1 or live_qty < 1:
            logger.warning(f"PME {ev.symbol}: no shares to trim (live_qty={live_qty})")
            return _result(ev, executed=False, qty=0, error="no_shares")

        order = tc.submit_order(_MktReq(
            symbol=ev.symbol,
            qty=qty,
            side=_Side.SELL,
            time_in_force=_TIF.DAY,
        ))
        logger.info(f"PME {ev.action} {ev.symbol} -{qty} sh ({pct*100:.0f}% of {live_qty}) | order_id={order.id}")
        notify(
            f"PME {ev.action}: {ev.symbol} -{qty} sh @ ~${ev.current_price:.2f} | "
            f"score={ev.score:.0f} R={ev.r_multiple:.2f}"
        )
        _mark_executed(ev)
        return _result(ev, executed=True, qty=qty, order_id=str(order.id))

    # ── EXIT ──────────────────────────────────────────────────────────────────

    def _do_exit(self, ev: PositionEvaluation) -> dict:
        tc = self._client.trading_client

        # Cancel all open orders for symbol
        cancelled = 0
        try:
            open_orders = tc.get_orders(filter=GetOrdersRequest(
                symbol=ev.symbol, status=QueryOrderStatus.OPEN
            ))
            for o in open_orders:
                try:
                    tc.cancel_order_by_id(str(o.id))
                    cancelled += 1
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"PME {ev.symbol}: could not cancel orders: {exc}")

        live_qty = _live_qty(self._client, ev.symbol)
        if live_qty < 1:
            logger.warning(f"PME {ev.symbol}: EXIT requested but no open position found")
            return _result(ev, executed=False, qty=0, error="no_position")

        order = tc.submit_order(_MktReq(
            symbol=ev.symbol,
            qty=live_qty,
            side=_Side.SELL,
            time_in_force=_TIF.DAY,
        ))
        logger.info(
            f"PME EXIT {ev.symbol} {live_qty} sh (cancelled {cancelled} orders) | order_id={order.id}"
        )
        notify(
            f"PME EXIT: {ev.symbol} {live_qty} sh @ ~${ev.current_price:.2f} | "
            f"R={ev.r_multiple:.2f} | {ev.reason}"
        )

        # Mark closed in DB
        try:
            close_trade(
                buy_order_id=ev.buy_order_id,
                exit_price=ev.current_price,
                exit_reason=f"pme_exit",
            )
        except Exception as exc:
            logger.warning(f"PME {ev.symbol}: DB close_trade failed: {exc}")

        _mark_executed(ev)
        return _result(ev, executed=True, qty=live_qty, order_id=str(order.id),
                       cancelled_orders=cancelled)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _live_qty(client: AlpacaClient, symbol: str) -> int:
    pos = client.get_position(symbol)
    if pos is None:
        return 0
    try:
        return abs(int(float(pos.qty)))
    except Exception:
        return 0


def _mark_executed(ev: PositionEvaluation) -> None:
    """Update the latest evaluation record to mark it executed."""
    try:
        evals = get_position_evaluations(buy_order_id=ev.buy_order_id)
        if evals:
            # The most recent one was just written by evaluate_all; update it
            save_position_evaluation({
                "symbol":         ev.symbol,
                "buy_order_id":   ev.buy_order_id,
                "score":          ev.score,
                "action":         ev.action,
                "r_multiple":     ev.r_multiple,
                "rs_vs_spy":      ev.rs_vs_spy,
                "trap_triggered": ev.trap_triggered,
                "reason":         ev.reason,
                "executed":       True,
            })
    except Exception:
        pass


def _result(
    ev: PositionEvaluation,
    *,
    executed: bool,
    qty: int,
    order_id: str = "",
    error: str = "",
    cancelled_orders: int = 0,
) -> dict:
    return {
        "symbol":           ev.symbol,
        "buy_order_id":     ev.buy_order_id,
        "action":           ev.action,
        "executed":         executed,
        "qty":              qty,
        "order_id":         order_id,
        "error":            error,
        "cancelled_orders": cancelled_orders,
        "score":            ev.score,
        "r_multiple":       ev.r_multiple,
        "reason":           ev.reason,
    }
