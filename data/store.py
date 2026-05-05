"""
SQLite persistence layer for scan results and trades.
All writes go through save_scan() and save_trade().
All reads go through the query_* functions used by the webapp.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent.parent / "data" / "atrades.db"


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                TEXT    NOT NULL,
                date              TEXT    NOT NULL,
                symbols_scanned   INTEGER DEFAULT 0,
                candidates_found  INTEGER DEFAULT 0,
                regime            TEXT,
                adx               REAL,
                spy_above_200ma   INTEGER,
                slope_20d         REAL,
                score_multiplier  REAL
            );

            CREATE TABLE IF NOT EXISTS scan_candidates (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_run_id    INTEGER NOT NULL,
                ts             TEXT    NOT NULL,
                date           TEXT    NOT NULL,
                symbol         TEXT,
                score          REAL,
                entry          REAL,
                stop           REAL,
                target         REAL,
                trail_atr      REAL,
                shares         INTEGER,
                partial_shares INTEGER,
                trail_shares   INTEGER,
                dollar_risk    REAL,
                risk_reward    REAL,
                volume_ratio   REAL,
                rsi            REAL,
                rs_vs_spy      REAL,
                is_trap        INTEGER DEFAULT 0,
                regime         TEXT,
                FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                date             TEXT NOT NULL,
                symbol           TEXT,
                buy_order_id     TEXT,
                partial_order_id TEXT,
                trail_order_id   TEXT,
                stop_order_id    TEXT,
                shares           INTEGER,
                partial_shares   INTEGER,
                trail_shares     INTEGER,
                entry            REAL,
                fill_price       REAL,
                fill_ts          TEXT,
                stop_loss        REAL,
                partial_target   REAL,
                trail_atr        REAL,
                score            REAL,
                status           TEXT DEFAULT 'open',
                exit_price       REAL,
                exit_ts          TEXT,
                exit_reason      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_scan_runs_date        ON scan_runs(date);
            CREATE INDEX IF NOT EXISTS idx_scan_candidates_date  ON scan_candidates(date);
            CREATE INDEX IF NOT EXISTS idx_trades_date           ON trades(date);
        """)
        # Migrate scan_candidates: add gap_pct if absent
        sc_cols = {r[1] for r in con.execute("PRAGMA table_info(scan_candidates)").fetchall()}
        if "gap_pct" not in sc_cols:
            con.execute("ALTER TABLE scan_candidates ADD COLUMN gap_pct REAL DEFAULT 0")

        # Migrate existing DB: add new columns if absent
        existing = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
        for col, defn in [
            ("stop_order_id", "TEXT"),
            ("fill_price",    "REAL"),
            ("fill_ts",       "TEXT"),
            ("status",        "TEXT DEFAULT 'open'"),
            ("exit_price",    "REAL"),
            ("exit_ts",       "TEXT"),
            ("exit_reason",   "TEXT"),
            ("actual_r",      "REAL"),
            ("hold_days",     "INTEGER"),
        ]:
            if col not in existing:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")


# ── Writes ────────────────────────────────────────────────────────────────────

def save_scan(
    candidates: list[dict],
    symbols_scanned: int,
    regime,          # MarketRegime dataclass
) -> None:
    """Persist one scan run and all its candidates."""
    now  = datetime.now(timezone.utc).isoformat()
    day  = date.today().isoformat()

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO scan_runs
               (ts, date, symbols_scanned, candidates_found,
                regime, adx, spy_above_200ma, slope_20d, score_multiplier)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                now, day, symbols_scanned, len(candidates),
                regime.state.value if regime else None,
                getattr(regime, "adx", None),
                int(getattr(regime, "spy_above_200ma", False)),
                getattr(regime, "spy_slope_20d", None),
                getattr(regime, "score_multiplier", None),
            ),
        )
        run_id = cur.lastrowid

        for c in candidates:
            con.execute(
                """INSERT INTO scan_candidates
                   (scan_run_id, ts, date, symbol, score, entry, stop, target,
                    trail_atr, shares, partial_shares, trail_shares,
                    dollar_risk, risk_reward, volume_ratio, rsi, rs_vs_spy,
                    is_trap, regime, gap_pct)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, now, day,
                    c["symbol"], c["score"], c["entry"], c["stop"], c["target"],
                    c.get("trail_atr", 0), c["shares"], c.get("partial_shares", 0),
                    c.get("trail_shares", 0), c["dollar_risk"], c["risk_reward"],
                    c["volume_ratio"], c["rsi"], c["rs_vs_spy"],
                    int(c.get("is_trap", False)),
                    c.get("regime", ""),
                    c.get("gap_pct", 0),
                ),
            )


def save_trade(order_result: dict) -> None:
    """Persist a placed order."""
    now = datetime.now(timezone.utc).isoformat()
    day = date.today().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO trades
               (ts, date, symbol, buy_order_id, partial_order_id, trail_order_id,
                stop_order_id, shares, partial_shares, trail_shares,
                entry, fill_price, fill_ts, stop_loss,
                partial_target, trail_atr, score, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now, day,
                order_result.get("symbol"),
                order_result.get("buy_order_id"),
                order_result.get("partial_order_id"),
                order_result.get("trail_order_id"),
                order_result.get("stop_order_id"),
                order_result.get("shares", 0),
                order_result.get("partial_shares", 0),
                order_result.get("trail_shares", 0),
                order_result.get("entry", 0),
                order_result.get("fill_price"),
                order_result.get("fill_ts"),
                order_result.get("stop_loss", 0),
                order_result.get("partial_target", 0),
                order_result.get("trail_atr", 0),
                order_result.get("score", 0),
                "open",
            ),
        )


def get_open_trades() -> list[dict]:
    """Return all trades with status 'open' or 'partial_exit'."""
    with _conn() as con:
        return _rows(con.execute(
            "SELECT * FROM trades WHERE status IN ('open','partial_exit') ORDER BY ts"
        ))


def update_trade_fill(buy_order_id: str, fill_price: float, fill_ts: str,
                      stop_order_id: str | None = None) -> None:
    """Record actual fill details after buy order confirms."""
    with _conn() as con:
        con.execute(
            """UPDATE trades
               SET fill_price=?, fill_ts=?, stop_order_id=COALESCE(?,stop_order_id)
               WHERE buy_order_id=?""",
            (fill_price, fill_ts, stop_order_id, buy_order_id),
        )


def upgrade_to_trailing(buy_order_id: str, trail_order_id: str,
                        stop_order_id_cleared: str) -> None:
    """After partial limit fills: record trail order, clear old stop, set status."""
    with _conn() as con:
        con.execute(
            """UPDATE trades
               SET trail_order_id=?, stop_order_id=NULL, status='partial_exit'
               WHERE buy_order_id=?""",
            (trail_order_id, buy_order_id),
        )


def close_trade(buy_order_id: str, exit_price: float,
                exit_reason: str) -> None:
    """Mark a trade closed and compute actual_r + hold_days."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT fill_price, stop_loss, ts FROM trades WHERE buy_order_id=?",
            (buy_order_id,),
        ).fetchone()

        actual_r  = None
        hold_days = None
        if row:
            fill_px   = row["fill_price"] or 0
            stop_loss = row["stop_loss"]  or 0
            risk      = fill_px - stop_loss
            if risk > 0 and exit_price:
                actual_r = round((exit_price - fill_px) / risk, 2)
            if row["ts"]:
                try:
                    open_dt  = datetime.fromisoformat(row["ts"])
                    close_dt = datetime.fromisoformat(now)
                    hold_days = max(0, (close_dt - open_dt).days)
                except Exception:
                    pass

        con.execute(
            """UPDATE trades
               SET status='closed', exit_price=?, exit_ts=?, exit_reason=?,
                   actual_r=?, hold_days=?
               WHERE buy_order_id=?""",
            (exit_price, now, exit_reason, actual_r, hold_days, buy_order_id),
        )


def query_performance(days: int = 90) -> dict:
    """Aggregate P&L stats for closed trades in the last N days."""
    with _conn() as con:
        rows = _rows(con.execute(
            """SELECT actual_r, hold_days, exit_reason, exit_price, fill_price,
                      stop_loss, symbol, ts
               FROM trades
               WHERE status='closed'
                 AND exit_ts >= datetime('now', ?)
                 AND actual_r IS NOT NULL""",
            (f"-{days} days",),
        ))

    if not rows:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": None,
            "avg_r": None, "avg_win_r": None, "avg_loss_r": None,
            "profit_factor": None, "avg_hold_days": None,
            "best_r": None, "worst_r": None,
            "by_exit_reason": {},
            "recent": [],
        }

    total   = len(rows)
    wins    = [r for r in rows if r["actual_r"] > 0]
    losses  = [r for r in rows if r["actual_r"] <= 0]
    all_r   = [r["actual_r"] for r in rows]
    gain_r  = sum(r["actual_r"] for r in wins)
    loss_r  = abs(sum(r["actual_r"] for r in losses)) or 1e-9

    by_reason: dict[str, int] = {}
    for r in rows:
        reason = r["exit_reason"] or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "total":          total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / total * 100, 1) if total else None,
        "avg_r":          round(sum(all_r) / total, 2) if total else None,
        "avg_win_r":      round(sum(r["actual_r"] for r in wins) / len(wins), 2) if wins else None,
        "avg_loss_r":     round(sum(r["actual_r"] for r in losses) / len(losses), 2) if losses else None,
        "profit_factor":  round(gain_r / loss_r, 2),
        "avg_hold_days":  round(sum(r["hold_days"] or 0 for r in rows) / total, 1) if total else None,
        "best_r":         round(max(all_r), 2),
        "worst_r":        round(min(all_r), 2),
        "by_exit_reason": by_reason,
        "recent":         rows[-10:],
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def query_day(day: str) -> dict:
    """Scan runs, latest-run candidates, and trades for a given date (YYYY-MM-DD)."""
    with _conn() as con:
        runs = _rows(con.execute(
            "SELECT * FROM scan_runs WHERE date=? ORDER BY ts DESC", (day,)
        ))
        latest_run    = runs[0] if runs else {}

        # All unique candidates for the day — keep highest-scoring row per symbol
        candidates = _rows(con.execute(
            """SELECT sc.*
               FROM scan_candidates sc
               WHERE sc.date=?
                 AND sc.id = (
                   SELECT sc2.id FROM scan_candidates sc2
                   WHERE sc2.date=? AND sc2.symbol=sc.symbol
                   ORDER BY sc2.score DESC LIMIT 1
                 )
               ORDER BY sc.score DESC""",
            (day, day),
        ))

        trades = _rows(con.execute(
            "SELECT * FROM trades WHERE date=? ORDER BY ts", (day,)
        ))

    return {
        "date":             day,
        "scan_count":       len(runs),
        "scan_times":       [r["ts"] for r in runs],
        "symbols_scanned":  latest_run.get("symbols_scanned", 0),
        "candidates_found": len(candidates),
        "trades_placed":    len(trades),
        "regime":           latest_run.get("regime"),
        "adx":              latest_run.get("adx"),
        "spy_above_200ma":  bool(latest_run.get("spy_above_200ma")),
        "slope_20d":        latest_run.get("slope_20d"),
        "score_multiplier": latest_run.get("score_multiplier"),
        "candidates":       candidates,
        "trades":           trades,
    }


def query_history(days: int = 30) -> list[dict]:
    """Per-day summary for the last N days."""
    with _conn() as con:
        rows = _rows(con.execute(
            """SELECT
                 r.date,
                 MAX(r.symbols_scanned)                        AS symbols_scanned,
                 COUNT(DISTINCT sc.symbol)                     AS candidates_found,
                 (SELECT COUNT(*) FROM trades t WHERE t.date = r.date) AS trades_placed,
                 MAX(r.regime)                                 AS regime,
                 MAX(r.adx)                                    AS adx,
                 MAX(r.score_multiplier)                       AS score_multiplier,
                 SUM(CASE WHEN sc.is_trap=0 THEN sc.dollar_risk ELSE 0 END) AS total_risk
               FROM scan_runs r
               LEFT JOIN scan_candidates sc ON sc.scan_run_id = r.id
               WHERE r.date >= date('now', ?)
               GROUP BY r.date
               ORDER BY r.date DESC""",
            (f"-{days} days",),
        ))
    return rows
