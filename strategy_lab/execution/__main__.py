"""Run a self-contained demo or watch Collector recordings with a separate paper ledger."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from ..amm import Reserves
from ..db import connect_readonly
from ..replay import RoundRecord
from .config import Settings
from .engine import Executor
from .paper import PaperBroker
from .signals import Recordings, Snapshot
from .store import Ledger


def demo():
    settings = Settings(enabled=True)
    ledger = Ledger(":memory:", settings)
    try:
        engine = Executor(settings, ledger, PaperBroker(ledger))
        start = 1789443000
        now = start + 600
        record = RoundRecord("BTC", start, start + 900, 100.0, "",
                             [(ts, 100.0 if ts < now else 100.2) for ts in range(start, now + 1, 5)],
                             [(now, Reserves.opening())])
        snapshot = Snapshot(record, "demo-pool", "demo-up", "demo-down", now, "graphql")
        engine.enter(snapshot, now)
        engine.advance(now, lambda position, ts: None)
        # Recreate the engine to demonstrate resuming from persisted position state.
        engine = Executor(replace(settings, enabled=False), ledger, PaperBroker(ledger))
        engine.advance(record.ending, lambda position, ts: "UP")
        print(json.dumps(ledger.summary(settings), indent=2))
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run an offline buy-to-redeem example")
    parser.add_argument("--config", help="non-secret JSON settings")
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--ledger", help="separate paper ledger path, one per wallet")
    parser.add_argument("--halt-file", help="presence stops new entries; reconciliation continues")
    parser.add_argument("--watch", action="store_true", help="repeat once per second instead of once")
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    if not args.config or not args.ledger:
        parser.error("provide --config and --ledger, or use --demo")
    settings = Settings.read(args.config)
    if Path(args.recordings).resolve() == Path(args.ledger).resolve():
        parser.error("execution ledger must be separate from Collector recordings")
    if Path(args.ledger).exists() and Path(args.recordings).exists():
        if Path(args.ledger).samefile(args.recordings):
            parser.error("execution ledger cannot alias Collector recordings")
    if not settings.enabled and not Path(args.ledger).exists():
        print("Paper execution disabled. Set enabled=true in the chosen paper config to rehearse.")
        return
    Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(args.ledger, settings)
    engine = Executor(settings, ledger, PaperBroker(ledger))
    last_report = None
    try:
        while True:
            now = int(time.time())
            conn = connect_readonly(args.recordings)
            try:
                conn.execute("BEGIN")
                readings = Recordings(conn)
                engine.advance(now, readings.settlement)
                halted = bool(args.halt_file and Path(args.halt_file).exists())
                snapshot = readings.current(settings.symbols[0], now)
                reason = engine.enter(snapshot, now, halted) if snapshot else "no live Round recorded"
                engine.advance(now, readings.settlement)
            finally:
                conn.close()
            report = json.dumps({"reason": reason, **ledger.summary(settings)})
            if report != last_report:
                print(report, flush=True)
                last_report = report
            if not args.watch:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
