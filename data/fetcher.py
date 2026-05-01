from datetime import datetime, timedelta
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger
import config

_TIMEFRAME_MAP = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


class DataFetcher:
    def __init__(self):
        self._client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )

    def get_bars(self, symbol: str, lookback_days: int = 5, timeframe: str = None) -> pd.DataFrame:
        tf = _TIMEFRAME_MAP.get(timeframe or config.TIMEFRAME, TimeFrame(1, TimeFrameUnit.Minute))
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=datetime.utcnow() - timedelta(days=lookback_days),
            end=datetime.utcnow(),
        )
        try:
            bars = self._client.get_stock_bars(request)
            df = bars.df
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol, level="symbol")
            df.index = pd.to_datetime(df.index)
            return df.rename(columns=str.lower)
        except Exception as exc:
            logger.error(f"Failed to fetch bars for {symbol}: {exc}")
            return pd.DataFrame()
