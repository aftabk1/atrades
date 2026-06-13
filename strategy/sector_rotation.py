"""
Sector rotation signal.

Measures relative strength of each SPDR sector ETF vs SPY over a rolling
20-day window, then scores a candidate based on whether money is flowing
INTO or OUT OF its sector.

A breakout in a top-3 sector is far more likely to hold than an isolated
move in a sector losing money flows — e.g. ORCL/NOW/AAPL all broke out
in June 2026 while Technology/Comm-Svcs were underperforming SPY.

Usage:
    from strategy.sector_rotation import SectorRotation
    sr = SectorRotation()                       # one instance per scan run
    sector_ranks = sr.get_ranks()               # one yfinance call, cached
    pts = sr.score_symbol("AAPL", sector_ranks) # per-candidate lookup
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf
from dataclasses import dataclass
from loguru import logger


# ── Sector ETF universe ───────────────────────────────────────────────────────

_SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI",
                "XLY", "XLP", "XLU", "XLRE", "XLB", "XLC"]

# yfinance sector string → SPDR ETF ticker
_SECTOR_TO_ETF: dict[str, str] = {
    "Technology":               "XLK",
    "Financial Services":       "XLF",
    "Healthcare":               "XLV",
    "Energy":                   "XLE",
    "Industrials":              "XLI",
    "Consumer Cyclical":        "XLY",
    "Consumer Defensive":       "XLP",
    "Utilities":                "XLU",
    "Real Estate":              "XLRE",
    "Basic Materials":          "XLB",
    "Communication Services":   "XLC",
}

# Hard-coded fallback map for the most-common S&P 500 symbols so we avoid
# slow per-symbol yfinance .info() calls during a live scan run.
_SYMBOL_TO_ETF: dict[str, str] = {
    # Technology
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AVGO":"XLK","ORCL":"XLK",
    "CRM":"XLK","ACN":"XLK","CSCO":"XLK","NOW":"XLK","AMAT":"XLK",
    "AMD":"XLK","INTC":"XLK","QCOM":"XLK","TXN":"XLK","MU":"XLK",
    "KLAC":"XLK","LRCX":"XLK","MRVL":"XLK","ADI":"XLK","CDNS":"XLK",
    "SNPS":"XLK","HPE":"XLK","WDC":"XLK","STX":"XLK","ANET":"XLK",
    "FFIV":"XLK","KEYS":"XLK","JNPR":"XLK","TER":"XLK","MPWR":"XLK",
    # Financials
    "JPM":"XLF","BAC":"XLF","WFC":"XLF","GS":"XLF","MS":"XLF",
    "BLK":"XLF","SCHW":"XLF","AXP":"XLF","COF":"XLF","USB":"XLF",
    "PNC":"XLF","TFC":"XLF","C":"XLF","MTB":"XLF","HBAN":"XLF",
    "AFL":"XLF","PRU":"XLF","MET":"XLF","AIG":"XLF","ALL":"XLF",
    "CB":"XLF","MMC":"XLF","AON":"XLF","IVZ":"XLF","BEN":"XLF",
    # Healthcare
    "UNH":"XLV","JNJ":"XLV","LLY":"XLV","ABT":"XLV","TMO":"XLV",
    "MRK":"XLV","ABBV":"XLV","DHR":"XLV","PFE":"XLV","AMGN":"XLV",
    "BMY":"XLV","GILD":"XLV","REGN":"XLV","VRTX":"XLV","SYK":"XLV",
    "MDT":"XLV","BSX":"XLV","ZBH":"XLV","EW":"XLV","DXCM":"XLV",
    # Energy
    "XOM":"XLE","CVX":"XLE","COP":"XLE","EOG":"XLE","SLB":"XLE",
    "MPC":"XLE","PSX":"XLE","VLO":"XLE","OXY":"XLE","PXD":"XLE",
    "DVN":"XLE","FANG":"XLE","HAL":"XLE","BKR":"XLE","OKE":"XLE",
    "KMI":"XLE","WMB":"XLE","TRGP":"XLE",
    # Industrials
    "GE":"XLI","RTX":"XLI","HON":"XLI","UPS":"XLI","CAT":"XLI",
    "DE":"XLI","MMM":"XLI","LMT":"XLI","GD":"XLI","NOC":"XLI",
    "BA":"XLI","FDX":"XLI","EMR":"XLI","ETN":"XLI","PH":"XLI",
    "AME":"XLI","ROK":"XLI","CHRW":"XLI","EXPD":"XLI","UNP":"XLI",
    "CSX":"XLI","NSC":"XLI","JBHT":"XLI",
    # Consumer Discretionary
    "AMZN":"XLY","TSLA":"XLY","HD":"XLY","MCD":"XLY","NKE":"XLY",
    "SBUX":"XLY","LOW":"XLY","TJX":"XLY","BKNG":"XLY","MAR":"XLY",
    "HLT":"XLY","YUM":"XLY","DRI":"XLY","CMG":"XLY","ROST":"XLY",
    "TGT":"XLY","BBY":"XLY","WSM":"XLY","MGM":"XLY","LVS":"XLY",
    # Consumer Staples
    "PG":"XLP","KO":"XLP","PEP":"XLP","COST":"XLP","WMT":"XLP",
    "PM":"XLP","MO":"XLP","CL":"XLP","KMB":"XLP","GIS":"XLP",
    "K":"XLP","CAG":"XLP","SJM":"XLP","MKC":"XLP","HRL":"XLP",
    "MDLZ":"XLP","KDP":"XLP","MNST":"XLP",
    # Utilities
    "NEE":"XLU","DUK":"XLU","SO":"XLU","D":"XLU","AEP":"XLU",
    "EXC":"XLU","XEL":"XLU","SRE":"XLU","PEG":"XLU","WEC":"XLU",
    "ES":"XLU","EIX":"XLU","ETR":"XLU","PPL":"XLU","CMS":"XLU",
    "LNT":"XLU","EVRG":"XLU","NI":"XLU","AEE":"XLU","CNP":"XLU",
    # Real Estate
    "AMT":"XLRE","PLD":"XLRE","CCI":"XLRE","EQIX":"XLRE","PSA":"XLRE",
    "DLR":"XLRE","O":"XLRE","WELL":"XLRE","AVB":"XLRE","EQR":"XLRE",
    "VTR":"XLRE","INVH":"XLRE","EXR":"XLRE","ARE":"XLRE","FRT":"XLRE",
    "UDR":"XLRE","NNN":"XLRE",
    # Materials
    "LIN":"XLB","APD":"XLB","ECL":"XLB","SHW":"XLB","FCX":"XLB",
    "NEM":"XLB","NUE":"XLB","VMC":"XLB","MLM":"XLB","ALB":"XLB",
    "CF":"XLB","MOS":"XLB","PPG":"XLB","AVY":"XLB","PKG":"XLB",
    # Communication Services
    "META":"XLC","GOOGL":"XLC","GOOG":"XLC","NFLX":"XLC","DIS":"XLC",
    "CMCSA":"XLC","TMUS":"XLC","VZ":"XLC","T":"XLC","CHTR":"XLC",
    "OMC":"XLC","IPG":"XLC","AKAM":"XLC","TTWO":"XLC","EA":"XLC",
    "WBD":"XLC","FOXA":"XLC","FOX":"XLC","PARA":"XLC","NWSA":"XLC",
}


@dataclass
class SectorRank:
    etf: str
    rs_vs_spy: float    # % outperformance vs SPY over lookback window
    rank: int           # 1 = best sector, 11 = worst


@dataclass
class SectorRotationResult:
    symbol: str
    sector_etf: str | None
    sector_rs: float        # sector RS vs SPY (%)
    sector_rank: int        # 1–11 (or 0 = unknown)
    score: float            # 0–1 composite used by scorer
    description: str


class SectorRotation:
    """
    Fetches sector ETF rankings once per scan and caches them for the day.
    Call `get_ranks()` at the start of a scan run, then `score_symbol()` per
    candidate — the symbol lookup is pure in-memory after the initial fetch.
    """

    def __init__(self, lookback_days: int = 20):
        self.lookback_days = lookback_days
        self._ranks: dict[str, SectorRank] = {}
        self._cache_date: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def get_ranks(self) -> dict[str, SectorRank]:
        """
        Fetch and rank all 11 sector ETFs by relative strength vs SPY.
        Result is cached per calendar day — safe to call inside the scan loop.
        """
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        if self._cache_date == today and self._ranks:
            return self._ranks

        self._ranks = self._fetch_and_rank()
        self._cache_date = today
        return self._ranks

    def score_symbol(
        self,
        symbol: str,
        ranks: dict[str, SectorRank],
    ) -> SectorRotationResult:
        """
        Score a symbol based on whether its sector has positive money flow.

        Returns a SectorRotationResult with a 0–1 composite score:
          1.0  — top 3 sector (strong inflows, follow the money)
          0.5  — middle 4 sectors (neutral)
          0.0  — bottom 4 or negative RS vs SPY (outflows, avoid)
         -0.5  — sector RS < -3% (strongly lagging, penalise)
        """
        etf = self._resolve_etf(symbol)
        if etf is None or etf not in ranks:
            return SectorRotationResult(symbol, None, 0.0, 0, 0.5,
                                        "Sector unknown — neutral")

        rank_obj = ranks[etf]
        rs       = rank_obj.rs_vs_spy
        rank     = rank_obj.rank

        if rank <= 3:
            score = 1.0
            desc = (f"Sector {etf} rank #{rank} of 11 — top-tier inflows "
                    f"(RS {rs:+.1f}% vs SPY)")
        elif rank <= 7:
            score = 0.5
            desc = (f"Sector {etf} rank #{rank} of 11 — neutral "
                    f"(RS {rs:+.1f}% vs SPY)")
        elif rs < -3.0:
            score = -0.5
            desc = (f"Sector {etf} rank #{rank} of 11 — strong outflows "
                    f"(RS {rs:+.1f}% vs SPY) — penalised")
        else:
            score = 0.0
            desc = (f"Sector {etf} rank #{rank} of 11 — lagging "
                    f"(RS {rs:+.1f}% vs SPY)")

        return SectorRotationResult(symbol, etf, rs, rank, score, desc)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fetch_and_rank(self) -> dict[str, SectorRank]:
        """Download 30 days of ETF prices and compute RS vs SPY for each sector."""
        tickers = _SECTOR_ETFS + ["SPY"]
        try:
            raw = yf.download(
                tickers,
                period="30d",
                interval="1d",
                progress=False,
                auto_adjust=True,
                timeout=15,
            )
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        except Exception as exc:
            logger.warning(f"SectorRotation: failed to fetch ETF data — {exc}")
            return {}

        lb = min(self.lookback_days, len(close) - 1)
        if lb < 5 or "SPY" not in close.columns:
            logger.warning("SectorRotation: insufficient ETF history")
            return {}

        spy_ret = float(close["SPY"].iloc[-1] / close["SPY"].iloc[-lb] - 1) * 100

        rs_list: list[tuple[str, float]] = []
        for etf in _SECTOR_ETFS:
            if etf not in close.columns:
                continue
            etf_ret = float(close[etf].iloc[-1] / close[etf].iloc[-lb] - 1) * 100
            rs_list.append((etf, round(etf_ret - spy_ret, 2)))

        rs_list.sort(key=lambda x: x[1], reverse=True)

        ranks: dict[str, SectorRank] = {}
        for i, (etf, rs) in enumerate(rs_list):
            ranks[etf] = SectorRank(etf=etf, rs_vs_spy=rs, rank=i + 1)

        logger.info(
            "SectorRotation ranks: "
            + " | ".join(f"{etf} #{r.rank} ({r.rs_vs_spy:+.1f}%)"
                         for etf, r in ranks.items())
        )
        return ranks

    def _resolve_etf(self, symbol: str) -> str | None:
        """Return the sector ETF for a symbol — hardcoded map first, then yfinance fallback."""
        etf = _SYMBOL_TO_ETF.get(symbol.upper())
        if etf:
            return etf
        # Slow path: yfinance .info() for unknown symbols
        try:
            sector_str = yf.Ticker(symbol).info.get("sector", "")
            return _SECTOR_TO_ETF.get(sector_str)
        except Exception:
            return None
