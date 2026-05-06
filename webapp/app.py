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

import hmac
import os
import secrets

import uvicorn
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Body, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from datetime import date as _date
from data.store import (
    init_db, query_day, query_history, get_open_trades,
    query_performance, get_position_evaluations,
    query_closed_trades, query_realized_pnl,
)

ENV_PATH = ROOT / ".env"

# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
# Set DASHBOARD_USER and DASHBOARD_PASS in .env (or Fly.io secrets).
# If not set, auth is disabled (safe for local-only use).

_AUTH_USER = os.getenv("DASHBOARD_USER", "")
_AUTH_PASS = os.getenv("DASHBOARD_PASS", "")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)


def _check_auth(request: Request) -> None:
    if not _AUTH_ENABLED:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic realm=\"A1TRADES\""},
        )
    import base64
    try:
        decoded   = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, pw = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic realm=\"A1TRADES\""})

    # Constant-time comparison prevents timing attacks
    user_ok = hmac.compare_digest(user.encode(), _AUTH_USER.encode())
    pass_ok = hmac.compare_digest(pw.encode(),   _AUTH_PASS.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"A1TRADES\""},
        )


_Auth = Depends(_check_auth)


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
    "SCORE_VOLUME_SURGE":              "20.0",
    "SCORE_BREAKOUT_20D":              "16.0",
    "SCORE_RELATIVE_STRENGTH":         "12.0",
    "SCORE_RSI_ZONE":                  "12.0",
    "SCORE_BREAKOUT_50D":              "12.0",
    "SCORE_ATR_EXPANSION":              "8.0",
    "SCORE_CONSOLIDATION":              "8.0",
    "SCORE_HIGHER_LOWS":                "8.0",
    "SCORE_EARNINGS_PROXIMITY":         "4.0",
    "SCORE_ACCUM_MAX_BONUS":           "12.0",
    "SCORE_TRAP_MAX_PENALTY":          "32.0",
    "REGIME_AWARE_SCANNING":           "true",
    "REGIME_OVERRIDE":                 "",
    "SCAN_MODE":                        "custom",
    "SCANNER_INTERVAL_MINUTES":        "5",
    "BACKTEST_MAX_HOLD_DAYS":          "20",
    "BACKTEST_SLIPPAGE_PCT":           "0.0005",
    "BACKTEST_INITIAL_CAPITAL":        "100000",
    "WHATSAPP_PHONE":                  "",
    "WHATSAPP_APIKEY":                 "",
    # Position Management Engine
    "PME_ADD_SCORE_THRESHOLD":         "75",
    "PME_HOLD_SCORE_MIN":              "60",
    "PME_TRIM_LIGHT_SCORE_MIN":        "55",
    "PME_TRIM_HEAVY_SCORE_MIN":        "45",
    "PME_ADD_SIZE_PCT":                "0.20",
    "PME_ADD_MAX_MULTIPLIER":          "1.50",
    "PME_TRIM_LIGHT_PCT":              "0.25",
    "PME_TRIM_HEAVY_PCT":              "0.60",
    "PME_RS_ADD_MIN_PCT":              "8.0",
    "PME_RS_DOWNGRADE_BELOW_PCT":      "2.0",
    "PME_R_TRIM_FLOOR":                "2.0",
    "PME_R_TRIM_ENFORCE":              "3.0",
    "PME_FOLLOWTHROUGH_DAYS":          "2",
    "PME_VOLUME_SELLOFF_MULT":         "2.0",
}


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(
    title="A1TRADES Dashboard",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    dependencies=[_Auth],   # enforces auth on every route
)

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
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
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


# ── Close Position API ────────────────────────────────────────────────────────

@app.post("/api/positions/close")
def api_close_position(body: dict = Body(...)):
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        return JSONResponse(status_code=400, content={"ok": False, "error": "symbol required"})
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest as _MktReq
        from alpaca.trading.enums import OrderSide as _Side, TimeInForce as _TIF, QueryOrderStatus

        tc = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
        )

        # 1. Cancel every open order for this symbol
        open_orders = tc.get_orders(filter=GetOrdersRequest(
            symbol=symbol, status=QueryOrderStatus.OPEN
        ))
        cancelled = []
        for o in open_orders:
            try:
                tc.cancel_order_by_id(str(o.id))
                cancelled.append(str(o.id))
            except Exception:
                pass

        # 2. Market sell the full position
        position = tc.get_open_position(symbol)
        qty = abs(float(position.qty))
        sell_order = tc.submit_order(_MktReq(
            symbol=symbol,
            qty=qty,
            side=_Side.SELL,
            time_in_force=_TIF.DAY,
        ))

        # 3. Mark trade closed in DB
        exit_price = float(position.current_price)
        try:
            from data.store import close_trade as _close_trade
            _close_trade(
                buy_order_id=body.get("buy_order_id", ""),
                exit_price=exit_price,
                exit_reason="manual_close",
            )
        except Exception:
            pass

        # 4. WhatsApp notification
        try:
            from notifications.whatsapp import notify as _notify
            import config as _cfg; importlib.reload(_cfg)
            _notify(f"TRADE CLOSED: {symbol} {qty:.0f} sh @ ${exit_price:.2f} (manual close)")
        except Exception:
            pass

        return {
            "ok": True,
            "symbol": symbol,
            "qty": qty,
            "cancelled_orders": len(cancelled),
            "sell_order_id": str(sell_order.id),
        }
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})


# ── Performance API ───────────────────────────────────────────────────────────

@app.get("/api/performance")
def api_performance(days: int = Query(default=90, ge=1, le=365)):
    return query_performance(days)


# ── Closed trades API ────────────────────────────────────────────────────────

@app.get("/api/closed-trades")
def api_closed_trades(
    date_str: str | None = Query(default=None, alias="date"),
    days: int = Query(default=30, ge=1, le=365),
):
    return {"trades": query_closed_trades(day=date_str, days=days)}


# ── Position evaluations API ──────────────────────────────────────────────────

@app.get("/api/position-evaluations")
def api_position_evaluations(
    buy_order_id: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
):
    return get_position_evaluations(buy_order_id=buy_order_id, days=days)


# ── Account API ───────────────────────────────────────────────────────────────

@app.get("/api/account")
def api_account():
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        tc = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
        )
        acct      = tc.get_account()
        positions = tc.get_all_positions()

        equity       = float(acct.equity)
        last_equity  = float(acct.last_equity)
        cash         = float(acct.cash)
        buying_power = float(acct.buying_power)
        today_pnl    = round(equity - last_equity, 2)
        today_pnl_pct = round((today_pnl / last_equity * 100) if last_equity else 0, 2)

        unrealized_pl  = sum(float(p.unrealized_pl)   for p in positions)
        open_exposure  = sum(float(p.market_value)     for p in positions)
        exposure_pct   = round(open_exposure / equity * 100, 1) if equity else 0

        realized_pnl = query_realized_pnl()

        return {
            "equity":              round(equity, 2),
            "cash":                round(cash, 2),
            "buying_power":        round(buying_power, 2),
            "today_pnl":           today_pnl,
            "today_pnl_pct":       today_pnl_pct,
            "unrealized_pl":       round(unrealized_pl, 2),
            "realized_pnl":        realized_pnl,
            "open_exposure":       round(open_exposure, 2),
            "open_exposure_pct":   exposure_pct,
            "open_positions_count": len(positions),
            "is_paper":            (env.get("IS_PAPER", "true") or "true").lower() != "false",
        }
    except Exception as exc:
        return JSONResponse(status_code=200, content={"error": str(exc)})


# ── Live Trades API ───────────────────────────────────────────────────────────

@app.get("/api/live-trades")
def api_live_trades():
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        tc = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
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
    return {k: env.get(k, v) or v for k, v in CONFIG_DEFAULTS.items()}


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
        # REGIME_OVERRIDE is a select that legitimately stores an empty string (= "Auto")
        _allow_empty = {"REGIME_OVERRIDE"}
        safe = {
            k: str(v) for k, v in body.items()
            if k in CONFIG_DEFAULTS and (str(v).strip() != "" or k in _allow_empty)
        }
        _write_env(safe)
        # Reload config in the running process so new values take effect immediately
        import config as _cfg
        importlib.reload(_cfg)
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


@app.post("/api/notifications/test")
def notifications_test(body: dict = Body(...)):
    import urllib.parse, urllib.request, urllib.error
    phone  = str(body.get("phone",  "")).strip()
    apikey = str(body.get("apikey", "")).strip()
    if not phone or not apikey:
        return JSONResponse(status_code=400, content={"ok": False, "error": "phone and apikey are required"})
    msg    = "A1TRADES: test notification — WhatsApp alerts are working."
    params = urllib.parse.urlencode({"phone": phone, "text": msg, "apikey": apikey})
    url    = f"https://api.callmebot.com/whatsapp.php?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "response": body_text}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": f"HTTP {exc.code}: {body_text or exc.reason}"},
        )
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})


@app.get("/api/scan/next")
def scan_next():
    """Return last scan timestamp and when the next one is due."""
    try:
        env = _parse_env(_read_env_lines())
        interval = int(env.get("SCANNER_INTERVAL_MINUTES", "5") or "5")
    except Exception:
        interval = 5

    import sqlite3 as _sq
    from pathlib import Path as _Path
    db = _Path(__file__).parent.parent / "data" / "atrades.db"
    last_ts = None
    try:
        con = _sq.connect(db)
        row = con.execute("SELECT ts FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            last_ts = row[0]
        con.close()
    except Exception:
        pass

    next_ts = None
    if last_ts:
        try:
            from datetime import timedelta
            last_dt = datetime.fromisoformat(last_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_ts = (last_dt + timedelta(minutes=interval)).isoformat()
        except Exception:
            pass

    return {
        "last_scan_ts":    last_ts,
        "next_scan_ts":    next_ts,
        "interval_minutes": interval,
        "runner_running":  _runner_alive(),
        "scan_running":    _scan_running,
    }


# ── Runner API ───────────────────────────────────────────────────────────────

_runner_proc: subprocess.Popen | None = None


def _runner_alive() -> bool:
    return _runner_proc is not None and _runner_proc.poll() is None


@app.get("/api/trading-mode")
def trading_mode():
    try:
        env = _parse_env(_read_env_lines())
        is_paper = (env.get("IS_PAPER", "true") or "true").lower() != "false"
        base_url = env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets") or "https://paper-api.alpaca.markets"
        return {"is_paper": is_paper, "base_url": base_url}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/runner/status")
def runner_status():
    try:
        env = _parse_env(_read_env_lines())
        is_paper = (env.get("IS_PAPER", "true") or "true").lower() != "false"
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
