from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order
from loguru import logger
from .alpaca_client import AlpacaClient


class OrderManager:
    def __init__(self, client: AlpacaClient):
        self._client = client

    def market_buy(self, symbol: str, qty: float) -> Order | None:
        return self._submit_market_order(symbol, qty, OrderSide.BUY)

    def market_sell(self, symbol: str, qty: float) -> Order | None:
        return self._submit_market_order(symbol, qty, OrderSide.SELL)

    def limit_buy(self, symbol: str, qty: float, limit_price: float) -> Order | None:
        return self._submit_limit_order(symbol, qty, limit_price, OrderSide.BUY)

    def limit_sell(self, symbol: str, qty: float, limit_price: float) -> Order | None:
        return self._submit_limit_order(symbol, qty, limit_price, OrderSide.SELL)

    def close_position(self, symbol: str) -> None:
        try:
            self._client.trading_client.close_position(symbol)
            logger.info(f"Closed position for {symbol}")
        except Exception as exc:
            logger.error(f"Failed to close {symbol}: {exc}")

    def _submit_market_order(self, symbol: str, qty: float, side: OrderSide) -> Order | None:
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.trading_client.submit_order(req)
            logger.info(f"Market {side.value} {qty} {symbol} — order {order.id}")
            return order
        except Exception as exc:
            logger.error(f"Market order failed ({symbol}): {exc}")
            return None

    def _submit_limit_order(self, symbol: str, qty: float, price: float, side: OrderSide) -> Order | None:
        try:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                limit_price=round(price, 2),
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.trading_client.submit_order(req)
            logger.info(f"Limit {side.value} {qty} {symbol} @ {price} — order {order.id}")
            return order
        except Exception as exc:
            logger.error(f"Limit order failed ({symbol}): {exc}")
            return None
