"""Fetch historical daily OHLCV data from Alpaca and save to CSV."""

from datetime import datetime, date
from pathlib import Path

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

import config

_DEFAULT_OUT = Path(__file__).parent.parent / "historical_data"


class HistoricalDataFetcher:
    def __init__(self, output_dir: Path | str = _DEFAULT_OUT):
        self._client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def fetch(
        self,
        symbols: list[str],
        start: date | str,
        end: date | str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Return {symbol: DataFrame} for all requested symbols."""
        start_dt = _to_datetime(start)
        end_dt = _to_datetime(end) if end else datetime.utcnow()

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
        )

        logger.info(
            f"Fetching daily bars for {symbols} "
            f"from {start_dt.date()} to {end_dt.date()}"
        )

        try:
            bars = self._client.get_stock_bars(request)
            df_all = bars.df
        except Exception as exc:
            logger.error(f"Alpaca request failed: {exc}")
            return {}

        results: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                df = df_all.xs(symbol, level="symbol").copy()
                df.index = pd.to_datetime(df.index).date
                df.index.name = "date"
                df.columns = [c.lower() for c in df.columns]
                results[symbol] = df
            except KeyError:
                logger.warning(f"No data returned for {symbol}")

        return results

    def save(self, data: dict[str, pd.DataFrame]) -> list[Path]:
        """Write each symbol's DataFrame to its own CSV. Returns saved paths."""
        saved: list[Path] = []
        for symbol, df in data.items():
            if df.empty:
                logger.warning(f"Skipping {symbol} — empty DataFrame")
                continue
            path = self.output_dir / f"{symbol}_daily.csv"
            df.to_csv(path)
            logger.info(f"Saved {len(df)} rows → {path}")
            saved.append(path)
        return saved

    def fetch_and_save(
        self,
        symbols: list[str],
        start: date | str,
        end: date | str | None = None,
    ) -> list[Path]:
        """Convenience method: fetch then save, return list of written paths."""
        data = self.fetch(symbols, start, end)
        return self.save(data)


def _to_datetime(value: date | str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.fromisoformat(str(value))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch historical daily data from Alpaca")
    parser.add_argument("symbols", nargs="+", help="Ticker symbols, e.g. AAPL MSFT GOOGL")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="Output directory")
    args = parser.parse_args()

    fetcher = HistoricalDataFetcher(output_dir=args.out)
    paths = fetcher.fetch_and_save(
        symbols=[s.upper() for s in args.symbols],
        start=args.start,
        end=args.end,
    )

    if paths:
        print(f"\nSaved {len(paths)} file(s):")
        for p in paths:
            print(f"  {p}")
    else:
        print("No data saved.")
