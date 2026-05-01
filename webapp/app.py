"""
ATrades Web Dashboard — FastAPI backend.

Endpoints:
  GET /                       → dashboard HTML
  GET /api/dashboard?date=    → scan + trade data for one day
  GET /api/history            → last 30 days summary
  GET /api/positions          → live Alpaca open positions

Usage:
  python -m webapp.app
  python webapp/app.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Add project root to path so imports work when run from any directory
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from data.store import init_db, query_day, query_history

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="ATrades Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(STATIC / "index.html"))


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def api_dashboard(date_str: str = Query(default=None, alias="date")):
    day = date_str or date.today().isoformat()
    return query_day(day)


@app.get("/api/history")
def api_history(days: int = Query(default=30, ge=1, le=365)):
    return query_history(days)


@app.get("/api/positions")
def api_positions():
    try:
        import config
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.IS_PAPER,
        )
        positions = client.get_all_positions()
        clock     = client.get_clock()
        return {
            "market_open": clock.is_open,
            "next_open":   clock.next_open.isoformat() if not clock.is_open else None,
            "positions": [
                {
                    "symbol":       p.symbol,
                    "qty":          float(p.qty),
                    "avg_entry":    float(p.avg_entry_price),
                    "current":      float(p.current_price),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc) * 100,
                    "side":         p.side.value,
                }
                for p in positions
            ],
        }
    except Exception as exc:
        return JSONResponse(status_code=200, content={"error": str(exc), "positions": []})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "webapp.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
