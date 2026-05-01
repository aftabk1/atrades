import schedule
import time
from loguru import logger

import config
from broker.alpaca_client import AlpacaClient
from broker.order_manager import OrderManager
from data.fetcher import DataFetcher
from data.processor import DataProcessor
from risk.manager import RiskManager
from strategy.moving_average import MovingAverageCrossStrategy
from strategy.rsi import RSIStrategy


def build_strategies() -> dict:
    return {
        symbol: [
            MovingAverageCrossStrategy(symbol, fast=10, slow=30),
            RSIStrategy(symbol, period=14),
        ]
        for symbol in config.SYMBOLS
    }


def run_cycle(
    client: AlpacaClient,
    orders: OrderManager,
    fetcher: DataFetcher,
    risk: RiskManager,
    strategies: dict,
) -> None:
    if not client.is_market_open():
        logger.info("Market is closed — skipping cycle")
        return

    if not risk.check_drawdown(stop_loss_pct=0.05):
        logger.warning("Drawdown limit hit — no new trades this cycle")
        return

    for symbol, symbol_strategies in strategies.items():
        df_raw = fetcher.get_bars(symbol, lookback_days=5)
        df = DataProcessor.clean(df_raw)
        df = DataProcessor.add_indicators(df)

        if df.empty:
            logger.warning(f"No data for {symbol}")
            continue

        current_price = float(df["close"].iloc[-1])
        signals = [s.generate_signal(df) for s in symbol_strategies]
        logger.debug(f"{symbol} signals: {signals}")

        # Require consensus among strategies
        if all(s == "buy" for s in signals):
            approved, qty = risk.approve_buy(symbol, current_price)
            if approved:
                orders.market_buy(symbol, qty)

        elif all(s == "sell" for s in signals):
            approved, qty = risk.approve_sell(symbol)
            if approved:
                orders.market_sell(symbol, qty)


def main() -> None:
    logger.info(f"ATrades starting — paper={config.IS_PAPER}, symbols={config.SYMBOLS}")

    client = AlpacaClient()
    orders = OrderManager(client)
    fetcher = DataFetcher()
    risk = RiskManager(client)
    strategies = build_strategies()

    def job():
        try:
            run_cycle(client, orders, fetcher, risk, strategies)
        except Exception as exc:
            logger.exception(f"Cycle error: {exc}")

    schedule.every(1).minutes.do(job)
    logger.info("Scheduler running — press Ctrl+C to stop")
    job()  # run immediately on startup

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
