import pandas as pd
import ta
from loguru import logger


class DataProcessor:
    @staticmethod
    def clean(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        df = df[df["volume"] > 0]
        return df.sort_index()

    @staticmethod
    def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 2:
            return df
        try:
            df["rsi"] = ta.momentum.RSIIndicator(close=df["close"]).rsi()
            bb = ta.volatility.BollingerBands(close=df["close"])
            df["bb_upper"] = bb.bollinger_hband()
            df["bb_lower"] = bb.bollinger_lband()
            macd = ta.trend.MACD(close=df["close"])
            df["macd"] = macd.macd()
            df["macd_signal"] = macd.macd_signal()
        except Exception as exc:
            logger.warning(f"Indicator calculation failed: {exc}")
        return df
