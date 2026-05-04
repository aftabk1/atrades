"""
Partial-exit order execution via Alpaca.

Entry + exit legs submitted as separate orders after fill confirmation:
  1. Market buy  — all shares (waits for fill)
  2. Stop-loss   — all shares at stop_price (GTC, immediate hard floor)
  3. Limit sell  — partial_shares at 2R target (GTC)

Once the position monitor detects the limit sell filled, it:
  4. Cancels the stop-loss order
  5. Places a trailing stop — trail_shares with trail_price = 2×ATR (GTC)
"""

from __future__ import annotations

import time

from loguru import logger

from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from broker.alpaca_client import AlpacaClient
from risk.trade_setup import TradeSetup

_FILL_POLL_INTERVAL = 2   # seconds between fill checks
_FILL_TIMEOUT       = 60  # max seconds to wait for buy fill


class BracketOrderExecutor:
    def __init__(self, client: AlpacaClient) -> None:
        self._client = client

    def place(self, setup: TradeSetup) -> dict | None:
        """
        Submit entry + exits for `setup`.
        Returns a result dict with order details, or None on failure.
        Skips gracefully when the market is closed.
        """
        if not self._client.is_market_open():
            logger.warning(f"Market closed — orders skipped for {setup.symbol}")
            return None

        if setup.shares < 1:
            logger.warning(f"Invalid share count ({setup.shares}) for {setup.symbol}")
            return None

        try:
            # ── 1. Market buy — full position ──────────────────────────────
            buy_req = MarketOrderRequest(
                symbol=setup.symbol,
                qty=setup.shares,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            buy_order = self._client.trading_client.submit_order(buy_req)
            logger.info(
                f"Buy submitted — {setup.symbol} {setup.shares} sh @ ~${setup.entry_price:.2f} "
                f"| order_id={buy_order.id}"
            )

            # ── 2. Wait for fill ───────────────────────────────────────────
            fill_price, fill_ts = self._wait_for_fill(setup.symbol, str(buy_order.id))
            if fill_price is None:
                logger.error(
                    f"{setup.symbol}: buy not filled within {_FILL_TIMEOUT}s — "
                    "exits not placed; position needs manual review"
                )
                return {
                    "buy_order_id": str(buy_order.id),
                    "symbol":       setup.symbol,
                    "shares":       setup.shares,
                    "entry":        setup.entry_price,
                    "fill_price":   None,
                    "stop_loss":    setup.stop_loss,
                    "partial_target": setup.target_price,
                    "trail_atr":    setup.trail_atr,
                    "score":        setup.score,
                    "status":       "fill_timeout",
                }

            # ── 3. Hard stop — all shares ──────────────────────────────────
            stop_order_id = None
            stop_req = StopOrderRequest(
                symbol=setup.symbol,
                qty=setup.shares,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(setup.stop_loss, 2),
            )
            stop_order = self._client.trading_client.submit_order(stop_req)
            stop_order_id = str(stop_order.id)
            logger.info(
                f"Hard stop — {setup.symbol} {setup.shares} sh "
                f"stop ${setup.stop_loss:.2f} | order_id={stop_order_id}"
            )

            # ── 4. Limit sell — partial_shares at 2R ──────────────────────
            partial_order_id = None
            if setup.partial_shares >= 1:
                limit_req = LimitOrderRequest(
                    symbol=setup.symbol,
                    qty=setup.partial_shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(setup.target_price, 2),
                )
                partial_order = self._client.trading_client.submit_order(limit_req)
                partial_order_id = str(partial_order.id)
                logger.info(
                    f"Partial exit — {setup.symbol} {setup.partial_shares} sh "
                    f"limit ${setup.target_price:.2f} | order_id={partial_order_id}"
                )

            return {
                "buy_order_id":     str(buy_order.id),
                "stop_order_id":    stop_order_id,
                "partial_order_id": partial_order_id,
                "trail_order_id":   None,
                "symbol":           setup.symbol,
                "shares":           setup.shares,
                "partial_shares":   setup.partial_shares,
                "trail_shares":     setup.trail_shares,
                "entry":            setup.entry_price,
                "fill_price":       fill_price,
                "fill_ts":          fill_ts,
                "stop_loss":        setup.stop_loss,
                "partial_target":   setup.target_price,
                "trail_atr":        setup.trail_atr,
                "score":            setup.score,
            }

        except Exception as exc:
            logger.error(f"Order submission failed for {setup.symbol}: {exc}")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _wait_for_fill(self, symbol: str, order_id: str) -> tuple[float | None, str | None]:
        """Poll until the order is filled or timeout. Returns (fill_price, fill_ts)."""
        deadline = time.time() + _FILL_TIMEOUT
        while time.time() < deadline:
            try:
                order = self._client.trading_client.get_order_by_id(order_id)
                if order.status == OrderStatus.FILLED:
                    fill_price = float(order.filled_avg_price)
                    fill_ts    = order.filled_at.isoformat() if order.filled_at else None
                    logger.info(
                        f"{symbol} filled @ ${fill_price:.2f} "
                        f"(expected ~${order.filled_avg_price})"
                    )
                    return fill_price, fill_ts
                if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                    OrderStatus.REJECTED):
                    logger.error(f"{symbol} buy order {order.status.value} — aborting exits")
                    return None, None
            except Exception as exc:
                logger.debug(f"Fill poll error for {symbol}: {exc}")
            time.sleep(_FILL_POLL_INTERVAL)
        return None, None
