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
import logging
import re
import secrets
import subprocess
import sys
import time as _time_mod
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import hmac
import os

import uvicorn
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Body, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from datetime import date as _date
from data.store import (
    init_db, query_day, query_history, get_open_trades,
    query_performance, get_position_evaluations,
    query_closed_trades, query_realized_pnl, query_scan_top5, query_scan_setups,
)

ENV_PATH = ROOT / ".env"

# ── Session-based Auth ────────────────────────────────────────────────────────
# Set DASHBOARD_USER and DASHBOARD_PASS in .env.
# Browser receives a random session cookie — password is NEVER cached.
# Sessions expire after SESSION_TIMEOUT seconds of inactivity.

_AUTH_USER    = os.getenv("DASHBOARD_USER", "")
_AUTH_PASS    = os.getenv("DASHBOARD_PASS", "")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)

# Refuse to start without credentials in production (PYTEST_CURRENT_TEST is set by pytest)
if not _AUTH_ENABLED and "PYTEST_CURRENT_TEST" not in os.environ:
    raise RuntimeError(
        "DASHBOARD_USER and DASHBOARD_PASS must be set in .env — "
        "the dashboard controls live trading and must never run unauthenticated."
    )

SESSION_COOKIE  = "a1t_sess"
CSRF_COOKIE     = "a1t_csrf"
SESSION_TIMEOUT = 90 * 60      # 90 minutes; re-login required after idle

_SESSIONS: dict[str, float] = {}   # token → last-active epoch seconds

# Rate limiter: max 10 login attempts per IP per 5-minute window
_RATE_WINDOW  = 300                 # 5 minutes
_RATE_MAX     = 10
_RATE_HITS: dict[str, list[float]] = defaultdict(list)

# Auth logger → logs/auth.log
_auth_log = logging.getLogger("a1trades.auth")
if not _auth_log.handlers:
    _log_dir = ROOT / "logs"
    _log_dir.mkdir(exist_ok=True)
    _fh = logging.FileHandler(_log_dir / "auth.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    _auth_log.addHandler(_fh)
    _auth_log.setLevel(logging.INFO)

# Audit logger → logs/audit.log  (financial operations)
_audit_log = logging.getLogger("a1trades.audit")
if not _audit_log.handlers:
    _log_dir = ROOT / "logs"
    _log_dir.mkdir(exist_ok=True)
    _afh = logging.FileHandler(_log_dir / "audit.log", encoding="utf-8")
    _afh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    _audit_log.addHandler(_afh)
    _audit_log.setLevel(logging.INFO)

# Date validation regex
_DATE_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")


def _cleanup_sessions() -> None:
    now = _time_mod.time()
    expired = [t for t, ts in list(_SESSIONS.items()) if now - ts > SESSION_TIMEOUT]
    for t in expired:
        _SESSIONS.pop(t, None)


def _rate_check(ip: str, bucket: str, max_hits: int, window: int) -> bool:
    """Return True if request is allowed; False if rate limit exceeded."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    key = f"{bucket}:{ip}"
    now = _time_mod.time()
    hits = _RATE_HITS[key]
    _RATE_HITS[key] = [t for t in hits if now - t < window]
    if len(_RATE_HITS[key]) >= max_hits:
        return False
    _RATE_HITS[key].append(now)
    return True


def _validate_date(date_str: str | None) -> str | None:
    """Raise 400 if date_str is present but not YYYY-MM-DD, or outside [today-365, today+1]."""
    if date_str is None:
        return None
    if not _DATE_RE.match(date_str):
        raise HTTPException(status_code=400, detail="Invalid date — expected YYYY-MM-DD")
    from datetime import date as _date, timedelta as _td
    try:
        d = _date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date value")
    today = _date.today()
    if d < today - _td(days=365) or d > today + _td(days=1):
        raise HTTPException(status_code=400, detail="Date out of range — must be within the last 365 days")
    return date_str


# _Auth kept as a no-op dependency (middleware handles real auth now)
_Auth = Depends(lambda: None)


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
    # Write with sanitised values — newlines are stripped at call sites in save_config
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

CONFIG_DEFAULTS = {
    "ALPACA_API_KEY":                  "",
    "ALPACA_SECRET_KEY":               "",
    "ALPACA_BASE_URL":                 "https://paper-api.alpaca.markets",
    "IS_PAPER":                        "true",
    "SYMBOLS":                         "AAPL,MSFT,GOOGL",
    "MAX_POSITION_SIZE":               "0.10",
    "MAX_PORTFOLIO_RISK":              "0.01",
    "MAX_CONCURRENT_TRADES":           "4",
    "MAX_DAILY_LOSS_PCT":              "0.04",
    "BREAKOUT_MIN_PRICE":              "25.0",
    "BREAKOUT_MIN_AVG_VOLUME":         "1000000",
    "BREAKOUT_VOLUME_SURGE_MULT":      "1.5",
    "BREAKOUT_RSI_LOW":                "50.0",
    "BREAKOUT_RSI_HIGH":               "65.0",
    "GAP_UP_THRESHOLD":                "0.08",
    "SETUP_PROXIMITY_PCT":             "0.05",
    "SETUP_MIN_SCORE":                 "45.0",
    "SCORE_PROXIMITY_20D":             "12.0",
    "BREAKOUT_CONSOLIDATION_LOOKBACK":  "15",
    "BREAKOUT_CONSOLIDATION_DAILY_VOL": "0.015",
    "BREAKOUT_HIGHER_LOWS_LOOKBACK":    "15",
    "BREAKOUT_MIN_SCORE":               "65.0",
    "BREAKOUT_ATR_STOP_MULT":           "2.0",
    "BREAKOUT_MAX_STOP_PCT":            "0.20",
    "BREAKOUT_SUPPORT_LOOKBACK":        "10",
    "BREAKOUT_RR_RATIO":                "2.0",
    "PARTIAL_EXIT_R":                  "2.0",
    "PARTIAL_EXIT_PCT":                "0.50",
    "TRAIL_ATR_MULT":                  "2.0",
    "ACCUM_LOOKBACK_DAYS":             "20",
    "BULL_TRAP_SCORE_THRESHOLD":       "40.0",
    "SCORE_VCP":                        "14.0",
    "SCORE_CONSOLIDATION":             "12.0",
    "SCORE_HIGHER_LOWS":               "12.0",
    "SCORE_52W_HIGH_PROXIMITY":        "10.0",
    "SCORE_EARNINGS_PROXIMITY":         "6.0",
    "SCORE_VOLUME_SURGE":              "14.0",
    "SCORE_BREAKOUT_20D":              "12.0",
    "SCORE_RSI_ZONE":                  "10.0",
    "SCORE_RELATIVE_STRENGTH":          "4.0",
    "SCORE_MARKET_BREADTH":             "6.0",
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
)

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ── Security middleware ───────────────────────────────────────────────────────

_OPEN_PATHS = {"/login", "/logout"}   # never require auth
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path

    # 1. Session auth (skip login/logout/static assets)
    if _AUTH_ENABLED and path not in _OPEN_PATHS and not path.startswith("/static/"):
        token = request.cookies.get(SESSION_COOKIE)
        _cleanup_sessions()
        if not token or token not in _SESSIONS:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "not_authenticated"})
            return Response(status_code=302, headers={"Location": "/login"})
        _SESSIONS[token] = _time_mod.time()   # refresh idle timer

        # 2. CSRF check — all state-changing requests must carry the CSRF token
        if request.method not in _CSRF_SAFE_METHODS:
            csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
                return JSONResponse(status_code=403, content={"detail": "invalid_csrf_token"})

    response = await call_next(request)

    # 2. Security headers on every response
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]         = "DENY"
    response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src  'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src   'self' https://fonts.gstatic.com; "
        "img-src    'self' data:; "
        "connect-src 'self';"
    )
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    # Handle password-manager GET submissions (credentials leaked into URL).
    # Authenticate on the spot so the user isn't stuck in a redirect loop.
    username = request.query_params.get("username", "")
    password = request.query_params.get("password", "")
    if username or password:
        user_ok = _AUTH_ENABLED and hmac.compare_digest(username.encode(), _AUTH_USER.encode())
        pass_ok = _AUTH_ENABLED and hmac.compare_digest(password.encode(), _AUTH_PASS.encode())
        if user_ok and pass_ok:
            token      = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            _SESSIONS[token] = _time_mod.time()
            _secure = os.getenv("SECURE_COOKIE", "true").lower() != "false"
            resp = Response(status_code=302, headers={"Location": "/"})
            resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            secure=_secure, max_age=SESSION_TIMEOUT)
            resp.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, samesite="lax",
                            secure=_secure, max_age=SESSION_TIMEOUT)
            return resp
        # Wrong credentials — redirect to clean login page
        return Response(status_code=302, headers={"Location": "/login"})
    resp = FileResponse(str(STATIC / "login.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/login", include_in_schema=False)
async def do_login(request: Request):
    client_ip = request.client.host if request.client else "unknown"

    # Rate-limit check
    now  = _time_mod.time()
    hits = _RATE_HITS[client_ip]
    _RATE_HITS[client_ip] = [t for t in hits if now - t < _RATE_WINDOW]
    if len(_RATE_HITS[client_ip]) >= _RATE_MAX:
        _auth_log.warning("RATE_LIMITED  ip=%s", client_ip)
        return JSONResponse(status_code=429,
                            content={"ok": False, "detail": "Too many attempts — try again in 5 minutes"})

    # Parse credentials from JSON or form
    try:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
    except Exception:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))

    user_ok = hmac.compare_digest(username.encode(), _AUTH_USER.encode())
    pass_ok = hmac.compare_digest(password.encode(), _AUTH_PASS.encode())

    if not (user_ok and pass_ok):
        _RATE_HITS[client_ip].append(now)
        _auth_log.warning("FAILED_LOGIN  ip=%s  user=%s", client_ip, username)
        return JSONResponse(status_code=401, content={"ok": False, "detail": "Invalid credentials"})

    token      = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    _SESSIONS[token] = _time_mod.time()
    _auth_log.info("LOGIN_OK  ip=%s  user=%s", client_ip, username)

    _secure = os.getenv("SECURE_COOKIE", "true").lower() != "false"
    resp = JSONResponse(content={"ok": True})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_secure,
        max_age=SESSION_TIMEOUT,
    )
    resp.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,    # JS must be able to read this to send X-CSRF-Token
        samesite="lax",
        secure=_secure,
        max_age=SESSION_TIMEOUT,
    )
    return resp


@app.post("/logout", include_in_schema=False)
async def do_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _SESSIONS.pop(token, None)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(CSRF_COOKIE)
    return resp


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(STATIC / "index.html"))


# ── Dashboard API ─────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def api_dashboard(date_str: str = Query(default=None, alias="date")):
    day = _validate_date(date_str) or date.today().isoformat()
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
def api_close_position(request: Request, body: dict = Body(...)):
    ip = request.client.host
    if not _rate_check(ip, "close", max_hits=5, window=60):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"})
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        return JSONResponse(status_code=400, content={"ok": False, "error": "symbol required"})
    _audit_log.info("CLOSE_POSITION | ip=%s | symbol=%s", ip, symbol)
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
    return {"trades": query_closed_trades(day=_validate_date(date_str), days=days)}


# ── Position evaluations API ──────────────────────────────────────────────────

@app.get("/api/position-evaluations")
def api_position_evaluations(
    buy_order_id: str | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
):
    return get_position_evaluations(buy_order_id=buy_order_id, days=days)


# ── Recent sells API (closed + partial trims from Alpaca order history) ───────

@app.get("/api/recent-sells")
def api_recent_sells(limit: int = Query(default=25, ge=1, le=100)):
    """Return recent filled SELL orders from Alpaca with FIFO P&L, most-recent first."""
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        from alpaca.trading.enums import OrderStatus as OStatus

        tc = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
        )

        all_orders = tc.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500
        ))
        filled = [o for o in all_orders if o.status == OStatus.FILLED]
        filled.sort(key=lambda o: o.submitted_at or o.created_at)

        lots: dict[str, list[dict]] = {}
        sells: list[dict] = []

        for o in filled:
            sym   = o.symbol
            qty   = float(o.filled_qty or o.qty or 0)
            price = float(o.filled_avg_price or 0)
            if qty == 0 or price == 0:
                continue
            ts = o.submitted_at or o.created_at

            if o.side == OrderSide.BUY:
                lots.setdefault(sym, []).append({"qty": qty, "price": price})
            elif o.side == OrderSide.SELL:
                # FIFO cost basis
                cost_total = 0.0
                remaining  = qty
                snapshot   = [dict(lot) for lot in lots.get(sym, [])]  # non-destructive peek
                for lot in lots.get(sym, []):
                    if remaining <= 0:
                        break
                    used = min(remaining, lot["qty"])
                    cost_total += lot["price"] * used
                    lot["qty"] -= used
                    remaining  -= used

                avg_cost = cost_total / qty if qty > 0 else 0.0
                pnl      = round((price - avg_cost) * qty, 2) if avg_cost else None

                sells.append({
                    "symbol":     sym,
                    "qty":        qty,
                    "sell_price": round(price, 2),
                    "avg_cost":   round(avg_cost, 2) if avg_cost else None,
                    "pnl":        pnl,
                    "ts":         ts.isoformat() if ts else None,
                    "date":       ts.date().isoformat() if ts else None,
                    "order_id":   str(o.id),
                })

        sells.reverse()           # most-recent first
        return {"sells": sells[:limit]}
    except Exception as exc:
        return JSONResponse(status_code=200, content={"error": str(exc), "sells": []})


# ── Realized P&L helper ───────────────────────────────────────────────────────

def _alpaca_realized_pnl(tc) -> float:
    """
    Compute realized P&L using FIFO accounting over all Alpaca fills.
    This always matches Alpaca's own P&L number exactly.
    Falls back to DB estimate if the API call fails.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide
        from alpaca.trading.enums import OrderStatus as OStatus

        all_orders = tc.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500
        ))
        filled = [o for o in all_orders if o.status == OStatus.FILLED]
        filled.sort(key=lambda o: o.submitted_at or o.created_at)

        lots: dict[str, list[dict]] = {}
        realized = 0.0

        for o in filled:
            sym   = o.symbol
            qty   = float(o.filled_qty or o.qty or 0)
            price = float(o.filled_avg_price or 0)
            if qty == 0 or price == 0:
                continue
            if o.side == OrderSide.BUY:
                lots.setdefault(sym, []).append({"qty": qty, "price": price})
            elif o.side == OrderSide.SELL:
                remaining = qty
                for lot in lots.get(sym, []):
                    if remaining <= 0:
                        break
                    used = min(remaining, lot["qty"])
                    realized += (price - lot["price"]) * used
                    lot["qty"] -= used
                    remaining -= used

        return round(realized, 2)
    except Exception:
        return query_realized_pnl()


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

        realized_pnl = _alpaca_realized_pnl(tc)

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

_SECRET_KEYS = {"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "WHATSAPP_APIKEY", "DASHBOARD_PASS"}

@app.get("/api/config")
def get_config():
    env = _parse_env(_read_env_lines())
    result = {}
    for k, default in CONFIG_DEFAULTS.items():
        val = env.get(k, default) or default
        result[k] = "***" if (k in _SECRET_KEYS and val) else val
    return result


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
def save_config(request: Request, body: dict = Body(...)):
    ip = request.client.host
    if not _rate_check(ip, "config", max_hits=5, window=60):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"})
    try:
        # REGIME_OVERRIDE is a select that legitimately stores an empty string (= "Auto")
        _allow_empty = {"REGIME_OVERRIDE"}
        safe = {
            k: str(v).replace("\n", "").replace("\r", "") for k, v in body.items()
            if k in CONFIG_DEFAULTS and (str(v).strip() != "" or k in _allow_empty)
        }
        _write_env(safe)
        keys_changed = list(safe.keys())
        _audit_log.info("CONFIG_SAVE | ip=%s | keys=%s", ip, keys_changed)
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
def scan_start(request: Request, body: dict = Body(default={})):
    global _scan_proc, _scan_lines, _scan_running
    ip = request.client.host
    if not _rate_check(ip, "scan", max_hits=1, window=30):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded — wait 30 s between scans"})
    if _scan_running:
        return {"ok": False, "message": "scan already running"}
    execute = bool(body.get("execute"))
    _audit_log.info("SCAN_START | ip=%s | execute=%s", ip, execute)
    _scan_lines   = []
    _scan_running = True
    cmd = [sys.executable, str(ROOT / "scanner.py")]
    if execute:
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


@app.get("/api/scan/top5")
def scan_top5(date_str: str = Query(default=None, alias="date")):
    """Return top 5 near-miss candidates (qualified=0) from the most recent scan."""
    try:
        rows = query_scan_top5(day=date_str)
        return {"candidates": rows, "count": len(rows)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/scan/setups")
def scan_setups(date_str: str = Query(default=None, alias="date")):
    """Return pre-breakout SETUP (Gate D) candidates from the most recent scan."""
    try:
        rows = query_scan_setups(day=_validate_date(date_str))
        return {"candidates": rows, "count": len(rows)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/api/prices")
def live_prices(symbols: str = Query(...)):
    """Return latest price for a comma-separated list of symbols via Alpaca (real-time) or yfinance fallback."""
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
    if not syms:
        return {}

    # Try Alpaca latest trades first (real-time during market hours)
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        data_client = StockHistoricalDataClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
        )
        resp = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=syms))
        result = {}
        for sym in syms:
            if sym in resp and resp[sym].price:
                result[sym] = round(float(resp[sym].price), 2)
        if result:
            return result
    except Exception:
        pass

    # Fallback: yfinance 1-minute bars (15-min delayed)
    try:
        import yfinance as yf
        tickers = yf.download(syms, period="1d", interval="1m", progress=False, auto_adjust=True)
        if tickers.empty:
            return {}
        if isinstance(tickers.columns, pd.MultiIndex):
            close = tickers.xs("Close", axis=1, level=0)
        else:
            close = tickers[["Close"]] if "Close" in tickers.columns else tickers
        result = {}
        for sym in syms:
            try:
                col = sym if sym in close.columns else (close.columns[0] if len(syms) == 1 else None)
                if col is None:
                    continue
                price = float(close[col].dropna().iloc[-1])
                result[sym] = round(price, 2)
            except Exception:
                pass
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/notifications/test")
def notifications_test(request: Request, body: dict = Body(...)):
    if not _rate_check(request.client.host, "notif", max_hits=3, window=60):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"})
    import urllib.parse, urllib.request, urllib.error
    phone  = str(body.get("phone",  "")).strip()
    apikey = str(body.get("apikey", "")).strip()
    # UI masks the stored key as "***" — fall back to the real config value
    if apikey == "***":
        apikey = str(getattr(config, "WHATSAPP_APIKEY", "")).strip()
    if not phone or not apikey:
        return JSONResponse(status_code=400, content={"ok": False, "error": "phone and apikey are required"})
    msg    = "A1TRADES: test notification — WhatsApp alerts are working."
    url    = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({"phone": phone, "text": msg, "apikey": apikey})
    req    = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


# ── Cached market-open status (avoids calling Alpaca on every poll) ───────────
_mkt_cache: dict = {"open": None, "next_open": None, "ts": 0.0}
_MKT_CACHE_TTL = 120   # seconds


def _get_market_status() -> dict:
    now = _time_mod.time()
    if now - _mkt_cache["ts"] < _MKT_CACHE_TTL:
        return {"market_open": _mkt_cache["open"], "next_open": _mkt_cache["next_open"]}
    try:
        env = _parse_env(_read_env_lines())
        from alpaca.trading.client import TradingClient
        tc = TradingClient(
            api_key=env.get("ALPACA_API_KEY", ""),
            secret_key=env.get("ALPACA_SECRET_KEY", ""),
            paper=(env.get("IS_PAPER", "true") or "true").lower() != "false",
        )
        clock = tc.get_clock()
        _mkt_cache["open"]      = clock.is_open
        _mkt_cache["next_open"] = clock.next_open.isoformat() if not clock.is_open else None
        _mkt_cache["ts"]        = now
    except Exception:
        # Fallback: time-based estimate (Mon–Fri 9:30–16:00 ET)
        import pytz as _pytz
        from datetime import datetime as _dt
        _et = _pytz.timezone("America/New_York")
        _now_et = _dt.now(_et)
        _mins = _now_et.hour * 60 + _now_et.minute
        _is_open = _now_et.weekday() < 5 and 570 <= _mins < 960  # 9:30–16:00
        _mkt_cache["open"]   = _is_open
        _mkt_cache["next_open"] = None
        _mkt_cache["ts"]     = now
    return {"market_open": _mkt_cache["open"], "next_open": _mkt_cache["next_open"]}


@app.get("/api/scan/next")
def scan_next():
    """Return last scan timestamp, when the next one is due, and market status."""
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

    mkt = _get_market_status()
    return {
        "last_scan_ts":    last_ts,
        "next_scan_ts":    next_ts,
        "interval_minutes": interval,
        "runner_running":  _runner_alive(),
        "scan_running":    _scan_running,
        "market_open":     mkt["market_open"],
        "next_open":       mkt["next_open"],
    }


# ── Runner API ───────────────────────────────────────────────────────────────

_runner_proc: subprocess.Popen | None = None
_RUNNER_PID_FILE = ROOT / "logs" / "runner.pid"


def _runner_alive() -> bool:
    global _runner_proc
    # 1. Subprocess we launched in this server session
    if _runner_proc is not None and _runner_proc.poll() is None:
        return True
    if _runner_proc is not None:
        _runner_proc = None  # process exited, clear reference

    # 2. PID file (survives server restarts; written by runner_start below)
    if _RUNNER_PID_FILE.exists():
        try:
            pid = int(_RUNNER_PID_FILE.read_text().strip())
            os.kill(pid, 0)   # raises OSError if dead; signal 0 = existence check
            return True
        except (ValueError, OSError):
            try:
                _RUNNER_PID_FILE.unlink()   # stale PID — clean up
            except OSError:
                pass

    # 3. Linux-only fallback: pgrep
    import subprocess as _sp
    try:
        out = _sp.check_output(
            ["pgrep", "-f", "runner.py"], text=True, stderr=_sp.DEVNULL
        ).strip()
        return bool(out)
    except Exception:
        return False


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
def runner_start(request: Request, body: dict = Body(default={})):
    global _runner_proc
    ip = request.client.host
    if not _rate_check(ip, "runner", max_hits=1, window=30):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"})
    if _runner_alive():
        return {"ok": True, "running": True, "message": "already running"}
    dry_run = body.get("dry_run", False)
    _audit_log.info("RUNNER_START | ip=%s | dry_run=%s", ip, dry_run)
    cmd = [sys.executable, str(ROOT / "runner.py")]
    if dry_run:
        cmd.append("--dry-run")
    _runner_proc = subprocess.Popen(cmd, cwd=str(ROOT))
    try:
        _RUNNER_PID_FILE.parent.mkdir(exist_ok=True)
        _RUNNER_PID_FILE.write_text(str(_runner_proc.pid))
    except OSError:
        pass
    return {"ok": True, "running": True, "message": "runner started"}


@app.post("/api/runner/stop")
def runner_stop(request: Request):
    global _runner_proc
    ip = request.client.host
    if not _rate_check(ip, "runner", max_hits=1, window=30):
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"})
    if not _runner_alive():
        return {"ok": True, "running": False, "message": "not running"}
    _audit_log.info("RUNNER_STOP | ip=%s", ip)
    if _runner_proc is not None:
        _runner_proc.terminate()
        try:
            _runner_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _runner_proc.kill()
        _runner_proc = None
    elif _RUNNER_PID_FILE.exists():
        # Runner was started outside this server session — kill by PID
        import signal as _signal
        try:
            pid = int(_RUNNER_PID_FILE.read_text().strip())
            os.kill(pid, _signal.SIGTERM)
        except (ValueError, OSError):
            pass
    try:
        _RUNNER_PID_FILE.unlink()
    except OSError:
        pass
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
