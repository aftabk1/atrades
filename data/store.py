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
                shares           INTEGER,
                partial_shares   INTEGER,
                trail_shares     INTEGER,
                entry            REAL,
                stop_loss        REAL,
                partial_target   REAL,
                trail_atr        REAL,
                score            REAL
            );

            CREATE INDEX IF NOT EXISTS idx_scan_runs_date        ON scan_runs(date);
            CREATE INDEX IF NOT EXISTS idx_scan_candidates_date  ON scan_candidates(date);
            CREATE INDEX IF NOT EXISTS idx_trades_date           ON trades(date);
        """)


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
                    is_trap, regime)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, now, day,
                    c["symbol"], c["score"], c["entry"], c["stop"], c["target"],
                    c.get("trail_atr", 0), c["shares"], c.get("partial_shares", 0),
                    c.get("trail_shares", 0), c["dollar_risk"], c["risk_reward"],
                    c["volume_ratio"], c["rsi"], c["rs_vs_spy"],
                    int(c.get("is_trap", False)),
                    c.get("regime", ""),
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
                shares, partial_shares, trail_shares, entry, stop_loss,
                partial_target, trail_atr, score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now, day,
                order_result.get("symbol"),
                order_result.get("buy_order_id"),
                order_result.get("partial_order_id"),
                order_result.get("trail_order_id"),
                order_result.get("shares", 0),
                order_result.get("partial_shares", 0),
                order_result.get("trail_shares", 0),
                order_result.get("entry", 0),
                order_result.get("stop_loss", 0),
                order_result.get("partial_target", 0),
                order_result.get("trail_atr", 0),
                order_result.get("score", 0),
            ),
        )


# ── Reads ─────────────────────────────────────────────────────────────────────

def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def query_day(day: str) -> dict:
    """All scan runs, candidates and trades for a given date (YYYY-MM-DD)."""
    with _conn() as con:
        runs = _rows(con.execute(
            "SELECT * FROM scan_runs WHERE date=? ORDER BY ts DESC", (day,)
        ))
        candidates = _rows(con.execute(
            """SELECT sc.*, sr.regime as run_regime
               FROM scan_candidates sc
               JOIN scan_runs sr ON sc.scan_run_id = sr.id
               WHERE sc.date=?
               ORDER BY sc.score DESC""",
            (day,),
        ))
        trades = _rows(con.execute(
            "SELECT * FROM trades WHERE date=? ORDER BY ts", (day,)
        ))

    # deduplicate candidates — keep best score per symbol per day
    seen: dict[str, dict] = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in seen or c["score"] > seen[sym]["score"]:
            seen[sym] = c
    unique_candidates = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    latest_run = runs[0] if runs else {}
    return {
        "date":             day,
        "scan_count":       len(runs),
        "symbols_scanned":  latest_run.get("symbols_scanned", 0),
        "candidates_found": len(unique_candidates),
        "trades_placed":    len(trades),
        "regime":           latest_run.get("regime"),
        "adx":              latest_run.get("adx"),
        "spy_above_200ma":  bool(latest_run.get("spy_above_200ma")),
        "slope_20d":        latest_run.get("slope_20d"),
        "score_multiplier": latest_run.get("score_multiplier"),
        "candidates":       unique_candidates,
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
