"""Explicit live entry point. Without --execute, only read-only preflight runs."""
import argparse
import fcntl
import os
import math
import sqlite3
from pathlib import Path
import tempfile
import time
from datetime import datetime

from ..db import connect_readonly
from .accounts import address, read_profiles
from .engine import Executor
from .errors import SetupError
from .live import LiveBroker, settings_from_profile
from .logging import LiveLog, market_status
from .signals import Recordings
from .store import Ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="execution-accounts.json")
    parser.add_argument("--symbol", choices=("BTC", "XYZCL"), required=True)
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--ledger", required=True, help="live ledger; never use the paper ledger")
    parser.add_argument("--halt-file", required=True, help="stops entries, keeps claims running")
    parser.add_argument("--execute", action="store_true", help="allow real buys and signed claims")
    parser.add_argument("--watch", action="store_true")
    deadline = parser.add_mutually_exclusive_group()
    deadline.add_argument("--overnight-hours", type=int, choices=range(1, 13), metavar="1..12",
                        help="start/resume a durable overnight session (10 USDC initial bankroll, recycled proceeds, 2 USDC loss stop)")
    deadline.add_argument("--continuous", action="store_true", help="persist continuous mode without resetting risk limits or existing positions")
    deadline.add_argument("--until", help="stop new entries at ISO date/time WITH offset, e.g. 2026-09-17T09:00:00+07:00")
    parser.add_argument("--log-format", choices=("text", "json"), default="text")
    parser.add_argument("--log-timezone", default="Asia/Bangkok")
    parser.add_argument("--log-interval", type=float, default=5, help="heartbeat seconds; new prices/states print immediately")
    parser.add_argument("--attach-buy", nargs=2, metavar=("POSITION_ID", "TX_HASH"))
    parser.add_argument("--retry-claim", metavar="POSITION_ID")
    args = parser.parse_args()
    os.umask(0o077)
    ledger = None
    lock = None
    stage = "public configuration"
    try:
        if not math.isfinite(args.log_interval) or args.log_interval <= 0:
            raise SetupError("LOG_INTERVAL: heartbeat must be positive seconds")
        log = LiveLog(args.log_format, args.log_timezone, args.log_interval)
        profile = read_profiles(args.config, selected_symbol=args.symbol)["wallets"][args.symbol]
        wallet = address(profile["address"])
        settings = settings_from_profile(profile, args.symbol, wallet)
        source, destination = Path(args.recordings), Path(args.ledger)
        if source.resolve() == destination.resolve() or (source.exists() and destination.exists() and source.samefile(destination)):
            raise ValueError("live ledger cannot be Collector database")
        if (args.attach_buy or args.retry_claim) and not args.execute:
            raise ValueError("recovery actions require --execute")
        # One process per wallet on this host, even across different ledger paths.
        stage = "wallet process lock (another runner may be active)"
        lock_path = Path(tempfile.gettempdir()) / f"9live-{os.getuid()}-{wallet}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = "ledger (check path, permissions and wallet/ledger identity)"
        destination.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(str(destination), settings)
        stage = "credentials and live limits"
        broker = LiveBroker(ledger, profile, args.symbol)
        stage = "read-only Arbitrum preflight"
        log.preflight(broker.preflight(), args.execute)
        if not args.execute:
            return
        stage = "live execution/recovery"
        if (args.continuous or args.overnight_hours is not None or args.until is not None) and not settings.enabled:
            raise SetupError("OVERNIGHT_DISABLED: enable the selected Symbol before starting the timed session")
        if args.continuous:
            broker.start_continuous()
        if args.overnight_hours is not None:
            broker.start_overnight(args.overnight_hours)
        if args.until is not None:
            try:
                stop_at = datetime.fromisoformat(args.until)
                if stop_at.utcoffset() is None:
                    raise ValueError()
            except ValueError:
                raise SetupError("OVERNIGHT_UNTIL: supply ISO date/time with a timezone offset") from None
            broker.start_until(int(stop_at.timestamp()))
        if args.attach_buy:
            broker.attach_buy(*args.attach_buy)
        if args.retry_claim:
            broker.retry_claim(args.retry_claim)
        engine = Executor(settings, ledger, broker)
        while True:
            now = int(time.time())
            engine.advance(now, broker.settlement)
            now = int(time.time())
            blocked = broker.entry_block()
            halted = Path(args.halt_file).exists()
            reason = blocked or ("new entries disabled" if halted or not settings.enabled else None)
            if reason is None and len(ledger.entry_active()) >= settings.max_open_positions:
                reason = "open-position limit"
            snapshot = None
            readings_failed = False
            try:
                conn = connect_readonly(args.recordings)
                try:
                    conn.execute("BEGIN")
                    snapshot = Recordings(conn).current(args.symbol, now)
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                readings_failed = True
            if reason is None:
                if readings_failed:
                    reason = "recordings unavailable"
                else:
                    reason = engine.enter(snapshot, now, halted) if snapshot else "no live Round recorded"
            engine.advance(int(time.time()), broker.settlement)
            summary = ledger.summary(settings)
            gas = ledger.conn.execute("SELECT COALESCE(SUM(gas_wei),0) FROM live_ops WHERE operation='redeem'").fetchone()[0]
            transactions = [dict(row) for row in ledger.conn.execute(
                "SELECT position_id,operation,tx_hash FROM live_ops ORDER BY position_id,operation")]
            logged_at = int(time.time())
            log.report({"reason": reason, "claim_gas_wei": gas,
                        "overnight": broker.overnight_status(), "min_quote_shares_micro": settings.min_quote_shares_micro,
                        "cash_excludes_eth_gas": True, "market": market_status(snapshot, logged_at, settings),
                        "transactions": transactions, **summary}, logged_at)
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    except SetupError as exc:
        parser.exit(2, f"Live runner stopped: {exc}. Do not delete pending positions.\n")
    except Exception as exc:
        # Never dump HTTP errors, environment values, signer objects or raw requests.
        parser.exit(2, f"Live runner stopped during {stage} ({type(exc).__name__}). Do not delete pending positions.\n")
    finally:
        if ledger:
            ledger.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
