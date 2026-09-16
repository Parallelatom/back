"""Explicit live entry point. Without --execute, only read-only preflight runs."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from ..db import connect_readonly
from .accounts import address, read_profiles
from .engine import Executor
from .live import LiveBroker, settings_from_profile
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
    parser.add_argument("--attach-buy", nargs=2, metavar=("POSITION_ID", "TX_HASH"))
    parser.add_argument("--retry-claim", metavar="POSITION_ID")
    args = parser.parse_args()
    os.umask(0o077)
    ledger = None
    lock = None
    try:
        profile = read_profiles(args.config)["wallets"][args.symbol]
        wallet = address(profile["address"])
        settings = settings_from_profile(profile, args.symbol, wallet)
        source, destination = Path(args.recordings), Path(args.ledger)
        if source.resolve() == destination.resolve() or (source.exists() and destination.exists() and source.samefile(destination)):
            raise ValueError("live ledger cannot be Collector database")
        if (args.attach_buy or args.retry_claim) and not args.execute:
            raise ValueError("recovery actions require --execute")
        # One process per wallet on this host, even across different ledger paths.
        lock_path = Path(tempfile.gettempdir()) / f"9live-{os.getuid()}-{wallet}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        destination.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(str(destination), settings)
        broker = LiveBroker(ledger, profile, args.symbol)
        print(json.dumps({"preflight": broker.preflight(), "execute": args.execute}), flush=True)
        if not args.execute:
            return
        if args.attach_buy:
            broker.attach_buy(*args.attach_buy)
        if args.retry_claim:
            broker.retry_claim(args.retry_claim)
        engine = Executor(settings, ledger, broker)
        last_report = None
        while True:
            now = int(time.time())
            engine.advance(now, broker.settlement)
            blocked = broker.entry_block()
            halted = Path(args.halt_file).exists()
            reason = blocked or ("new entries disabled" if halted or not settings.enabled else None)
            if reason is None:
                conn = connect_readonly(args.recordings)
                try:
                    conn.execute("BEGIN")
                    snapshot = Recordings(conn).current(args.symbol, now)
                finally:
                    conn.close()
                reason = engine.enter(snapshot, now, halted) if snapshot else "no live Round recorded"
            engine.advance(int(time.time()), broker.settlement)
            summary = ledger.summary(settings)
            gas = ledger.conn.execute("SELECT COALESCE(SUM(gas_wei),0) FROM live_ops WHERE operation='redeem'").fetchone()[0]
            report = json.dumps({"reason": reason, "claim_gas_wei": gas,
                                 "cash_excludes_eth_gas": True, **summary})
            if report != last_report:
                print(report, flush=True)
                last_report = report
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Never dump HTTP errors, environment values, signer objects or raw requests.
        parser.exit(2, f"Live runner stopped ({type(exc).__name__}). Check config, credentials, balances, lock and ledger; do not delete pending positions.\n")
    finally:
        if ledger:
            ledger.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
