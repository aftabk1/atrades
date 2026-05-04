"""
A1TRADES Web Dashboard — FastAPI backend.

Endpoints:
  GET  /                        → dashboard HTML
  GET  /api/dashboard?date=     → scan + trade data for one day
  GET  /api/history             → last 30 days summary
  GET  /api/positions           → live Alpaca open positions
  GET  /api/config              → read .env configuration
  POST /api/config              → write .env configuration

Usage:
  python -m webapp.app
  python webapp/app.py
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from datetime import date as _date
from data.store import init_db, query_day, query_history, get_open_trades, query_performance

ENV_PATH = ROOT / ".env"

# ── .env helpers ──────────────────────────────────────────────────────────────

def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()

def _parse_env(lines: list[str]) -> dict:
    result = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            key, _, val = s.partition("=")
            result[key.strip()] = val.strip()
    return result

def _write_env(updates: dict) -> None:
    lines     = _read_env_lines()
    updated   = set()
    new_lines = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

CONFIG_DEFAULTS = {
    "ALPACA_API_KEY":                  "",
    "ALPACA_SECRET_KEY":               "",
    "ALPACA_BASE_URL":                 "https://paper-api.alpaca.markets",
    "IS_PAPER":                        "true",
    "SYMBOLS":                         "AAPL,MSFT,GOOGL",
    "TIMEFRAME":                       "1Min",
    "MAX_POSITION_SIZE":               "0.10",
    "MAX_PORTFOLIO_RISK":              "0.01",
    "MAX_CONCURRENT_TRADES":           "4",
    "MAX_DAILY_LOSS_PCT":              "0.04",
    "BREAKOUT_MIN_PRICE":              "25.0",
    "BREAKOUT_MIN_AVG_VOLUME":         "1000000",
    "BREAKOUT_VOLUME_SURGE_MULT":      "1.5",
    "BREAKOUT_RSI_LOW":                "55.0",
    "BREAKOUT_RSI_HIGH":               "70.0",
    "BREAKOUT_ATR_EXPANSION_THRESHOLD":"1.2",
    "BREAKOUT_CONSOLIDATION_LOOKBACK": "15",
    "BREAKOUT_HIGHER_LOWS_LOOKBACK":   "15",
    "BREAKOUT_MIN_SCORE":              "60.0",
    "BREAKOUT_ATR_STOP_MULT":          "2.0",
    "BREAKOUT_MAX_STOP_PCT":           "0.20",
    "BREAKOUT_RR_RATIO":               "2.0",
    "PARTIAL_EXIT_R":                  "2.0",
    "PARTIAL_EXIT_PCT":                "0.50",
    "TRAIL_ATR_MULT":                  "2.0",
    "ACCUM_LOOKBACK_DAYS":             "20",
    "BULL_TRAP_SCORE_THRESHOLD":       "40.0",
    "REGIME_AWARE_SCANNING":           "true",
    "REGIME_OVERRIDE":                 "",
    "SCAN_MODE":                        "custom",
    "SCANNER_INTERVAL_MINUTES":        "5",
    "BACKTEST_MAX_HOLD_DAYS":          "20",
    "BACKTEST_SLIPPAGE_PCT":           "0.0005",
    "BACKTEST_INITIAL_CAPITAL":        "100000",
}


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="A1TRADES Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(STATIC / "index.html"))


# ── Dashboard API ─────────────────────────────────────────────────────────────

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
                    "symbol":          p.symbol,
                    "qty":             float(p.qty),
                    "avg_entry":       float(p.avg_entry_price),
                    "current":         float(p.current_price),
                    "market_value":    float(p.market_value),
                    "unrealized_pl":   float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc) * 100,
                    "side":            p.side.value,
                }
                for p in positions
            ],
        }
    except Exception as exc:
        return JSONResponse(status_code=200, content={"error": str(exc), "positions": []})


# ── Performance API ───────────────────────────────────────────────────────────

@app.get("/api/performance")
def api_performance(days: int = Query(default=90, ge=1, le=365)):
    return query_performance(days)


# ── Live Trades API ───────────────────────────────────────────────────────────

@app.get("/api/live-trades")
def api_live_trades():
    try:
        import config as _cfg
        importlib.reload(_cfg)
        from alpaca.trading.client import TradingClient
        tc = TradingClient(
            api_key=_cfg.ALPACA_API_KEY,
            secret_key=_cfg.ALPACA_SECRET_KEY,
            paper=_cfg.IS_PAPER,
        )

        # Live positions from Alpaca, keyed by symbol
        alpaca_pos = {p.symbol: p for p in tc.get_all_positions()}

        # DB open trades
        db_trades = {t["symbol"]: t for t in get_open_trades()}

        # Merge: start from Alpaca positions (source of truth), enrich with DB
        merged = []
        today = _date.today().isoformat()

        for symbol, pos in alpaca_pos.items():
            db = db_trades.get(symbol, {})
            fill_px   = db.get("fill_price") or float(pos.avg_entry_price)
            stop_loss = db.get("stop_loss") or 0.0
            target    = db.get("partial_target") or 0.0
            risk_per  = (fill_px - stop_loss) if stop_loss else 0.0
            current   = float(pos.current_price)
            unreal_pl = float(pos.unrealized_pl)
            unreal_r  = ((current - fill_px) / risk_per) if risk_per > 0 else None

            trade_date = db.get("date") or today
            try:
                from datetime import date as _d
                days_held = (_d.fromisoformat(today) - _d.fromisoformat(trade_date)).days
            except Exception:
                days_held = 0

            stop_dist_pct   = ((current - stop_loss) / current * 100) if stop_loss and current else None
            target_dist_pct = ((target - current) / current * 100) if target and current else None

            merged.append({
                "symbol":          symbol,
                "qty":             float(pos.qty),
                "fill_price":      round(fill_px, 2),
                "current_price":   round(current, 2),
                "unrealized_pl":   round(unreal_pl, 2),
                "unrealized_plpc": round(float(pos.unrealized_plpc) * 100, 2),
                "unrealized_r":    round(unreal_r, 2) if unreal_r is not None else None,
                "stop_loss":       round(stop_loss, 2) if stop_loss else None,
                "partial_target":  round(target, 2) if target else None,
                "stop_dist_pct":   round(stop_dist_pct, 1) if stop_dist_pct is not None else None,
                "target_dist_pct": round(target_dist_pct, 1) if target_dist_pct is not None else None,
                "days_held":       days_held,
                "status":          db.get("status", "open"),
                "score":           db.get("score"),
                "in_db":           bool(db),
            })

        # Sort by unrealized R descending
        merged.sort(key=lambda x: x["unrealized_r"] or -99, reverse=True)
        return {"trades": merged, "count": len(merged)}

    except Exception as exc:
        return JSONResponse(status_code=200, content={"error": str(exc), "trades": []})


# ── Config API ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    env = _parse_env(_read_env_lines())
    return {k: env.get(k, v) for k, v in CONFIG_DEFAULTS.items()}


@app.get("/api/symbols/fallback")
def get_fallback_symbols():
    try:
        from data.universe import _FALLBACK_SYMBOLS, _OVERRIDE_PATH
        if _OVERRIDE_PATH.exists():
            import json as _json
            symbols = _json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
            return {"symbols": symbols, "is_custom": True, "default_count": len(_FALLBACK_SYMBOLS)}
        return {"symbols": list(_FALLBACK_SYMBOLS), "is_custom": False, "default_count": len(_FALLBACK_SYMBOLS)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc), "symbols": []})


@app.post("/api/symbols/fallback")
def save_fallback_symbols(body: dict = Body(...)):
    try:
        import json as _json
        from data.universe import _OVERRIDE_PATH
        symbols = body.get("symbols", [])
        if not isinstance(symbols, list):
            return JSONResponse(status_code=400, content={"ok": False, "error": "symbols must be a list"})
        _OVERRIDE_PATH.write_text(_json.dumps(symbols), encoding="utf-8")
        return {"ok": True, "count": len(symbols)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.delete("/api/symbols/fallback/reset")
def reset_fallback_symbols():
    try:
        from data.universe import _OVERRIDE_PATH, _FALLBACK_SYMBOLS
        if _OVERRIDE_PATH.exists():
            _OVERRIDE_PATH.unlink()
        return {"ok": True, "count": len(_FALLBACK_SYMBOLS)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/api/config")
def save_config(body: dict = Body(...)):
    try:
        safe = {k: str(v) for k, v in body.items() if k in CONFIG_DEFAULTS}
        _write_env(safe)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


# ── Scan API ─────────────────────────────────────────────────────────────────

import threading

_scan_proc:    subprocess.Popen | None = None
_scan_lines:   list[str] = []
_scan_running: bool      = False


@app.post("/api/scan/start")
def scan_start(body: dict = Body(default={})):
    global _scan_proc, _scan_lines, _scan_running
    if _scan_running:
        return {"ok": False, "message": "scan already running"}
    _scan_lines   = []
    _scan_running = True
    cmd = [sys.executable, str(ROOT / "scanner.py")]
    if body.get("execute"):
        cmd.append("--execute")
    _scan_proc    = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(ROOT),
        encoding="utf-8", errors="replace",
    )

    def _reader():
        global _scan_running
        for line in _scan_proc.stdout:
            _scan_lines.append(line.rstrip())
        _scan_proc.wait()
        _scan_running = False

    threading.Thread(target=_reader, daemon=True).start()
    return {"ok": True}


@app.get("/api/scan/output")
def scan_output(offset: int = Query(default=0)):
    return {
        "lines":   _scan_lines[offset:],
        "offset":  len(_scan_lines),
        "running": _scan_running,
    }


# ── Runner API ───────────────────────────────────────────────────────────────

_runner_proc: subprocess.Popen | None = None


def _runner_alive() -> bool:
    return _runner_proc is not None and _runner_proc.poll() is None


@app.get("/api/trading-mode")
def trading_mode():
    try:
        import config as _cfg
        importlib.reload(_cfg)
        return {"is_paper": _cfg.IS_PAPER, "base_url": _cfg.ALPACA_BASE_URL}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/runner/status")
def runner_status():
    try:
        import config as _cfg
        importlib.reload(_cfg)
        is_paper = _cfg.IS_PAPER
    except Exception:
        is_paper = True
    return {"running": _runner_alive(), "is_paper": is_paper}


@app.post("/api/runner/start")
def runner_start(body: dict = Body(default={})):
    global _runner_proc
    if _runner_alive():
        return {"ok": True, "running": True, "message": "already running"}
    dry_run = body.get("dry_run", False)
    cmd = [sys.executable, str(ROOT / "runner.py")]
    if dry_run:
        cmd.append("--dry-run")
    _runner_proc = subprocess.Popen(cmd, cwd=str(ROOT))
    return {"ok": True, "running": True, "message": "runner started"}


@app.post("/api/runner/stop")
def runner_stop():
    global _runner_proc
    if not _runner_alive():
        return {"ok": True, "running": False, "message": "not running"}
    _runner_proc.terminate()
    try:
        _runner_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _runner_proc.kill()
    _runner_proc = None
    return {"ok": True, "running": False, "message": "runner stopped"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "webapp.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
