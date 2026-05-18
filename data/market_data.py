"""
Market data client.
Primary source: Alpaca StockHistoricalDataClient (batched, up to 50 symbols per call).
Fallback:       yfinance (auto-triggered for any symbol Alpaca couldn't supply).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

import config

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False
    logger.warning("yfinance not installed — fallback data source and earnings dates unavailable")

_ALPACA_BATCH = 50   # Alpaca's recommended max symbols per request
_YF_FAILURES: dict[str, int] = {}  # circuit-breaker: symbol → consecutive fail count
_YF_FAIL_LIMIT = 3


class MarketDataClient:
    def __init__(self) -> None:
        self._client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_daily_bars(
        self,
        symbols: list[str],
        days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        """
        Return {symbol: OHLCV DataFrame} with a DatetimeIndex normalized to midnight.
        Alpaca is tried first; yfinance fills any gaps.
        """
        start = datetime.now(timezone.utc) - timedelta(days=days + 10)  # +10 for weekend buffer
        results = self._alpaca_daily(symbols, start)

        missing = [s for s in symbols if s not in results or results[s].empty]
        if missing and _YF_OK:
            logger.info(f"yfinance fallback for {len(missing)} symbol(s)")
            results.update(self._yfinance_daily(missing, days))

        return results

    def get_latest_bars(self, symbols: list[str]) -> dict[str, dict]:
        """Return the most recent bar snapshot for each symbol."""
        try:
            req = StockLatestBarRequest(symbol_or_symbols=symbols)
            bars = self._client.get_stock_latest_bar(req)
            return {
                sym: {
                    "price": float(bar.close),
                    "volume": float(bar.volume),
                    "vwap": float(bar.vwap) if bar.vwap else float(bar.close),
                }
                for sym, bar in bars.items()
            }
        except Exception as exc:
            logger.warning(f"Latest bar fetch failed: {exc}")
            return {}

    def get_spy_data(self, days: int = 120) -> pd.DataFrame:
        data = self.get_daily_bars(["SPY"], days=days)
        spy = data.get("SPY", pd.DataFrame())
        if spy.empty:
            logger.error("SPY data unavailable — relative-strength signals will be skipped")
        return spy

    def get_earnings_date(self, symbol: str) -> Optional[datetime]:
        """Best-effort next earnings date via yfinance. Returns None on any failure."""
        if not _YF_OK or _YF_FAILURES.get(symbol, 0) >= _YF_FAIL_LIMIT:
            return None
        try:
            cal = yf.Ticker(symbol).calendar
            if cal is None or (hasattr(cal, "empty") and cal.empty):
                return None
            # calendar is a DataFrame; first column, first row = Earnings Date
            val = cal.iloc[0, 0]
            return pd.to_datetime(val).to_pydatetime() if pd.notna(val) else None
        except Exception:
            _YF_FAILURES[symbol] = _YF_FAILURES.get(symbol, 0) + 1
            return None

    def get_earnings_dates_bulk(
        self, symbols: list[str], max_workers: int = 10
    ) -> dict[str, Optional[datetime]]:
        """Fetch earnings dates for all symbols concurrently. Much faster than one-by-one."""
        results: dict[str, Optional[datetime]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.get_earnings_date, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    results[sym] = future.result()
                except Exception:
                    results[sym] = None
        return results

    # ── Private helpers ───────────────────────────────────────────────────────

    def _alpaca_daily(
        self, symbols: list[str], start: datetime
    ) -> dict[str, pd.DataFrame]:
        results: dict[str, pd.DataFrame] = {}
        end = datetime.now(timezone.utc)

        for i in range(0, len(symbols), _ALPACA_BATCH):
            batch = symbols[i : i + _ALPACA_BATCH]
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                )
                raw = self._client.get_stock_bars(req).df
                if raw.empty:
                    continue

                for sym in batch:
                    try:
                        df = raw.xs(sym, level="symbol").copy()
                        results[sym] = _normalise(df)
                    except KeyError:
                        pass

            except Exception as exc:
                logger.debug(f"Alpaca batch {batch[:3]}... failed: {exc}")

        return results

    def _yfinance_daily(
        self, symbols: list[str], days: int
    ) -> dict[str, pd.DataFrame]:
        results: dict[str, pd.DataFrame] = {}
        start_str = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%d")

        # filter circuit-broken symbols
        active = [s for s in symbols if _YF_FAILURES.get(s, 0) < _YF_FAIL_LIMIT]
        if not active:
            return results

        try:
            raw = yf.download(
                active,
                start=start_str,
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                return results

            for sym in active:
                try:
                    df = raw.xs(sym, axis=1, level=1).dropna(how="all")
                    df.columns = [c.lower() for c in df.columns]
                    results[sym] = _normalise(df)
                    _YF_FAILURES.pop(sym, None)  # reset on success
                except (KeyError, TypeError):
                    _YF_FAILURES[sym] = _YF_FAILURES.get(sym, 0) + 1

        except Exception as exc:
            logger.warning(f"yfinance batch download failed: {exc}")
            for sym in active:
                _YF_FAILURES[sym] = _YF_FAILURES.get(sym, 0) + 1

        return results


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure lowercase columns, timezone-naive midnight DatetimeIndex, sorted ascending."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df.sort_index()
