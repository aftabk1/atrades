"""
A1TRADES Autonomous Runner

Runs the breakout scanner during US market hours, places trades automatically,
and sleeps between sessions. No manual intervention needed.

Behaviour:
  - Waits for market open each day
  - Scans SCAN_AFTER_OPEN_MINS after open (default: 5 min) so opening prints settle
  - Re-scans every RESCAN_INTERVAL_MINS during the session (default: 60 min)
  - Executes bracket orders automatically for every qualifying setup
  - Skips weekends, holidays, and pre/post-market
  - Recovers from transient errors and keeps running

Usage:
    python runner.py                        # default: scan 5 min after open, rescan hourly
    python runner.py --interval 30          # rescan every 30 min
    python runner.py --interval 0           # scan once at open only
    python runner.py --dry-run              # scan + print, no orders placed

Windows autostart (Task Scheduler):
    schtasks /create /tn "A1TRADES" /sc daily /st 09:20 /tr "python C:\\projects\\atrades\\runner.py" /f
    schtasks /delete /tn "A1TRADES" /f
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timedelta

import pytz
from loguru import logger

import config
from notifications.whatsapp import notify
from data.store import init_db, save_scan, save_trade
from execution.position_executor import PositionExecutor
from execution.position_monitor import (
    check_circuit_breaker,
    ratchet_trailing_stops,
    reconcile_orphans,
    sync_open_trades,
)
from scanner import BreakoutScanner
from strategy.position_manager import PositionManager

# ── Constants ─────────────────────────────────────────────────────────────────
NY = pytz.timezone("America/New_York")
SCAN_AFTER_OPEN_MINS  = 5   # wait this long after open before first scan
POLL_SECS             = 30  # how often to check the clock while waiting
POSITION_CHECK_MINS   = 10  # intraday position sync interval (between scans)


# ── Logging setup ─────────────────────────────────────────────────────────────
def _configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
    )
    logger.add(
        "logs/runner_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )


# ── Market clock helpers ───────────────────────────────────────────────────────

def _get_clock(scanner: BreakoutScanner):
    return scanner._broker.trading_client.get_clock()


def _wait_for_open(scanner: BreakoutScanner) -> None:
    """Block until Alpaca reports the market is open."""
    while True:
        clock = _get_clock(scanner)
        if clock.is_open:
            return

        next_open_utc = clock.next_open
        now_utc       = datetime.now(pytz.utc)
        secs_to_open  = (next_open_utc - now_utc).total_seconds()
        next_open_ny  = next_open_utc.astimezone(NY)

        if secs_to_open <= 0:
            time.sleep(POLL_SECS)
            continue

        if secs_to_open > 3600:
            hrs = secs_to_open / 3600
            logger.info(
                f"Market closed. Next open: {next_open_ny.strftime('%a %Y-%m-%d %H:%M ET')} "
                f"({hrs:.1f}h away) -- sleeping 1h"
            )
            time.sleep(3600)
        elif secs_to_open > 300:
            logger.info(
                f"Market opens at {next_open_ny.strftime('%H:%M ET')} "
                f"({secs_to_open/60:.0f} min) -- sleeping 5 min"
            )
            time.sleep(300)
        else:
            logger.info(f"Market opens in {secs_to_open:.0f}s -- standing by")
            time.sleep(POLL_SECS)


# ── Session loop ──────────────────────────────────────────────────────────────

def run_session(scanner: BreakoutScanner, rescan_interval: int) -> None:
    """
    Run one full trading session:
      1. Wait SCAN_AFTER_OPEN_MINS after open for prices to settle
      2. Scan + execute
      3. Re-scan every rescan_interval minutes until market closes
    """
    now_ny = datetime.now(NY)
    logger.info(f"Market open -- waiting {SCAN_AFTER_OPEN_MINS} min for prices to settle")
    time.sleep(SCAN_AFTER_OPEN_MINS * 60)

    scan_count          = 0
    last_scan           = datetime.min.replace(tzinfo=NY)
    last_position_check = datetime.min.replace(tzinfo=NY)

    while True:
        clock = _get_clock(scanner)

        if not clock.is_open:
            logger.info(f"Market closed after {scan_count} scan(s) today")
            _end_of_day(scanner)
            return

        now            = datetime.now(NY)
        mins_since     = (now - last_scan).total_seconds() / 60
        due_for_rescan = (rescan_interval > 0 and mins_since >= rescan_interval)
        first_scan     = scan_count == 0

        # Intraday position monitor — runs every POSITION_CHECK_MINS between scans
        if scanner._execute:
            mins_since_check = (now - last_position_check).total_seconds() / 60
            if mins_since_check >= POSITION_CHECK_MINS and not (first_scan or due_for_rescan):
                try:
                    sync_open_trades(scanner._broker)
                    reconcile_orphans(scanner._broker)
                    last_position_check = datetime.now(NY)
                except Exception as exc:
                    logger.warning(f"Position monitor error (non-fatal): {exc}")

        if first_scan or due_for_rescan:
            try:
                _run_scan(scanner, scan_count)
                scan_count          += 1
                last_scan            = datetime.now(NY)
                last_position_check  = last_scan
            except Exception as exc:
                logger.error(f"Scan error (will retry next interval): {exc}")

            if rescan_interval == 0:
                # one scan per session — wait for close
                _wait_for_close(scanner)
                _end_of_day(scanner)
                return

        # Sleep until next poll or next scheduled scan
        sleep_secs = POLL_SECS if rescan_interval == 0 else min(
            max(rescan_interval * 60 - (datetime.now(NY) - last_scan).total_seconds(), POLL_SECS),
            300,
        )
        time.sleep(sleep_secs)


def _run_pme(scanner: BreakoutScanner, execute: bool = True) -> None:
    """Evaluate open positions via PME and optionally execute actions."""
    try:
        mgr      = PositionManager()
        evals    = mgr.evaluate_all()
        if not evals:
            return
        if execute and scanner._execute:
            executor = PositionExecutor(scanner._broker)
            for ev in evals:
                executor.execute(ev)
    except Exception as exc:
        logger.warning(f"PME error (non-fatal): {exc}")


def _run_scan(scanner: BreakoutScanner, scan_num: int) -> None:
    now_ny = datetime.now(NY)
    label  = "Opening scan" if scan_num == 0 else f"Re-scan #{scan_num}"
    logger.info(f"--- {label}  {now_ny.strftime('%H:%M ET')} ---")

    # ── PME: evaluate + act on existing positions before scanning for new ones
    if scanner._execute:
        logger.info("PME: evaluating open positions before scan")
        _run_pme(scanner, execute=True)

    # Sync position state before scanning
    if scanner._execute:
        try:
            sync_open_trades(scanner._broker)
        except Exception as exc:
            logger.warning(f"Position sync error (non-fatal): {exc}")

        # Circuit breaker — skip new trades if daily loss limit hit
        if check_circuit_breaker(scanner._broker):
            logger.warning("Circuit breaker active — scan runs but no new orders")
            scanner_execute_orig = scanner._execute
            scanner._execute = False
            candidates = scanner.scan()
            scanner.print_table(candidates)
            scanner._execute = scanner_execute_orig
            universe_size = len(scanner._universe.get_symbols())
            save_scan(candidates, universe_size, scanner._last_regime)
            return

    candidates = scanner.scan()
    scanner.print_table(candidates)

    # Persist scan results
    universe_size = len(scanner._universe.get_symbols())
    save_scan(candidates, universe_size, scanner._last_regime)

    if not candidates:
        logger.info("No qualifying setups this scan")
    else:
        logger.info(f"{len(candidates)} setup(s) found" + (" -- orders placed" if scanner._execute else " -- dry run, no orders"))


def _wait_for_close(scanner: BreakoutScanner) -> None:
    """Sleep in chunks until the market closes."""
    logger.info("Single-scan mode -- waiting for market close")
    while True:
        time.sleep(300)
        if not _get_clock(scanner).is_open:
            return


def _end_of_day(scanner: BreakoutScanner) -> None:
    """Post-close housekeeping: sync positions, ratchet trailing stops, record PME."""
    mode = "LIVE" if (scanner._execute and not config.IS_PAPER) else ("PAPER" if scanner._execute else "DRY RUN")
    notify(
        f"A1TRADES Session ENDED\n"
        f"Mode: {mode}\n"
        f"Time: {datetime.now(NY).strftime('%Y-%m-%d %H:%M ET')}"
    )
    if not scanner._execute:
        return
    try:
        sync_open_trades(scanner._broker)
        ratchet_trailing_stops(scanner._broker)
    except Exception as exc:
        logger.warning(f"End-of-day monitor error (non-fatal): {exc}")
    # PME end-of-day: evaluate and record recommendations (no execution — market closed)
    logger.info("PME: end-of-day position evaluation (record only)")
    _run_pme(scanner, execute=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="A1TRADES autonomous trading runner")
    _default_interval = int(getattr(config, "SCANNER_INTERVAL_MINUTES", 60))
    p.add_argument("--interval", type=int, default=_default_interval,
                   help=f"Re-scan interval in minutes (0 = once at open only, default: {_default_interval} from config)")
    p.add_argument("--dry-run",  action="store_true",
                   help="Scan and print setups but do not place orders")
    args = p.parse_args()

    _configure_logging()

    execute = not args.dry_run
    if args.dry_run:
        mode = "DRY RUN (no orders)"
    elif config.IS_PAPER:
        mode = "PAPER TRADING"
    else:
        mode = "*** REAL MONEY TRADING — LIVE ACCOUNT ***"
    rescan  = f"every {args.interval} min" if args.interval > 0 else "once at open"

    logger.info("=" * 58)
    logger.info(f"  A1TRADES Autonomous Runner")
    logger.info(f"  Mode     : {mode}")
    logger.info(f"  Scan     : {rescan}")
    logger.info(f"  Universe : {config.MAX_CONCURRENT_TRADES} max positions | "
                f"risk {config.MAX_PORTFOLIO_RISK:.0%}/trade")
    logger.info("=" * 58)

    # WhatsApp: runner started
    notify(
        f"A1TRADES Runner STARTED\n"
        f"Mode: {mode}\n"
        f"Scan: {rescan}\n"
        f"Time: {datetime.now(NY).strftime('%Y-%m-%d %H:%M ET')}"
    )

    # SIGTERM handler so pkill/systemctl stop also sends a shutdown message
    def _on_sigterm(signum, frame):
        logger.info("Runner received SIGTERM — shutting down")
        notify(
            f"A1TRADES Runner STOPPED (SIGTERM)\n"
            f"Time: {datetime.now(NY).strftime('%Y-%m-%d %H:%M ET')}"
        )
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    if execute and not config.IS_PAPER:
        logger.warning("!" * 58)
        logger.warning("  LIVE TRADING ACTIVE — orders will use REAL MONEY")
        logger.warning(f"  Account: {config.ALPACA_BASE_URL}")
        logger.warning("  Starting in 10 seconds. Press Ctrl+C to abort.")
        logger.warning("!" * 58)
        time.sleep(10)

    # Create logs directory and initialise DB
    import os
    os.makedirs("logs", exist_ok=True)
    init_db()

    scanner = BreakoutScanner(execute=execute)

    try:
        while True:
            try:
                _wait_for_open(scanner)
                run_session(scanner, rescan_interval=args.interval)
            except KeyboardInterrupt:
                logger.info("Runner stopped by user")
                notify(
                    f"A1TRADES Runner STOPPED (manual)\n"
                    f"Time: {datetime.now(NY).strftime('%Y-%m-%d %H:%M ET')}"
                )
                break
            except Exception as exc:
                logger.error(f"Unexpected error: {exc} -- restarting in 60s")
                time.sleep(60)
    except SystemExit:
        raise  # SIGTERM handler already sent notification


if __name__ == "__main__":
    main()
