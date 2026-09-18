"""Read-only analysis of addresses receiving newly minted 9Lives shares."""
import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..db import connect_readonly
from .accounts import TRANSFER, address, hex_value
from .live import RPC


def _topic_address(value):
    return "0x" + hex_value(value, 32)[-40:]


def _block_at_or_after(rpc, timestamp):
    latest = int(rpc.call("eth_blockNumber", []), 16)
    latest_ts = int(rpc.call("eth_getBlockByNumber", [hex(latest), False])["timestamp"], 16)
    if timestamp > latest_ts:
        raise ValueError("analysis end is later than the latest chain block")
    low, high = 0, latest
    while low < high:
        mid = (low + high) // 2
        block = rpc.call("eth_getBlockByNumber", [hex(mid), False])
        if not block:
            raise ValueError("RPC could not read a historical block")
        if int(block["timestamp"], 16) < timestamp:
            low = mid + 1
        else:
            high = mid
    return low


def analyse(conn, rpc, symbol, started_at, ends_at):
    if type(started_at) is not int or type(ends_at) is not int or not started_at < ends_at:
        raise ValueError("invalid analysis interval")
    if ends_at - started_at > 3 * 86400:
        raise ValueError("analyse no more than three days per run")
    rounds = [dict(row) for row in conn.execute(
        """SELECT symbol,starting,ending,pool_address,outcome_up,outcome_down
             FROM rounds WHERE symbol=? AND ending>? AND COALESCE(starting,ending-900)<?
               AND pool_address IS NOT NULL AND outcome_up IS NOT NULL AND outcome_down IS NOT NULL
             ORDER BY ending""", (symbol, started_at, ends_at))]
    shares = {}
    for row in rounds:
        pool = address(row["pool_address"])
        for side, outcome in (("UP", row["outcome_up"]), ("DOWN", row["outcome_down"])):
            raw = bytes.fromhex(hex_value(outcome, 8)[2:])
            share = address(rpc.view(pool, "shareAddr(bytes8)", ("bytes8",), (raw,), ("address",))[0])
            shares[share] = {**row, "pool_address": pool, "side": side, "share_token": share}
    if not shares:
        return []
    first_block = _block_at_or_after(rpc, started_at)
    last_block = _block_at_or_after(rpc, ends_at)
    events = []
    share_list = sorted(shares)
    zero_topic = "0x" + "0" * 64
    # Keep requests bounded for public RPC endpoints.
    for block_start in range(first_block, last_block + 1, 20_000):
        block_end = min(last_block, block_start + 19_999)
        for offset in range(0, len(share_list), 40):
            logs = rpc.call("eth_getLogs", [{
                "fromBlock": hex(block_start), "toBlock": hex(block_end),
                "address": share_list[offset:offset + 40],
                "topics": [TRANSFER, zero_topic],
            }])
            for log in logs:
                if log.get("removed") or len(log.get("topics", ())) != 3:
                    continue
                context = shares.get(log["address"].lower())
                if not context:
                    continue
                block_number = int(log["blockNumber"], 16)
                block = rpc.call("eth_getBlockByNumber", [hex(block_number), False])
                ts = int(block["timestamp"], 16)
                if not started_at <= ts < ends_at:
                    continue
                tx_hash = hex_value(log["transactionHash"], 32)
                tx = rpc.call("eth_getTransactionByHash", [tx_hash])
                ending = context["ending"]
                starting = context["starting"] or ending - 900
                if not starting <= ts <= ending:
                    continue
                events.append({
                    "timestamp": ts, "round_starting": starting, "round_ending": ending,
                    "seconds_to_end": ending - ts,
                    "relative_to_window_open_seconds": ts - (ending - 300),
                    "inside_live_window": 75 <= ending - ts <= 300,
                    "side": context["side"], "recipient": _topic_address(log["topics"][2]),
                    "tx_from": address(tx["from"]), "shares": int(log["data"], 16) / 1e6,
                    "pool": context["pool_address"], "share_token": context["share_token"],
                    "tx_hash": tx_hash,
                })
    return sorted(events, key=lambda row: (row["timestamp"], row["tx_hash"]))


def summarise(events):
    groups = defaultdict(lambda: {"buys": 0, "rounds": set(), "seconds_to_end": [], "shares": 0.0,
                                  "senders": set()})
    for event in events:
        group = groups[event["recipient"]]
        group["buys"] += 1
        group["rounds"].add(event["round_ending"])
        group["seconds_to_end"].append(event["seconds_to_end"])
        group["shares"] += event["shares"]
        group["senders"].add(event["tx_from"])
    result = []
    for recipient, group in groups.items():
        times = sorted(group["seconds_to_end"])
        result.append({"recipient": recipient, "buys": group["buys"], "rounds": len(group["rounds"]),
                       "median_seconds_to_end": times[len(times) // 2],
                       "min_seconds_to_end": min(times), "max_seconds_to_end": max(times),
                       "shares": group["shares"], "tx_from": ",".join(sorted(group["senders"]))})
    return sorted(result, key=lambda row: (-row["rounds"], -row["buys"], row["recipient"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("BTC", "XYZCL"), default="XYZCL")
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--from", dest="started_at", required=True, help="ISO time with UTC offset")
    parser.add_argument("--to", dest="ends_at", required=True, help="ISO time with UTC offset")
    parser.add_argument("--rpc-url", default="https://arb1.arbitrum.io/rpc")
    parser.add_argument("--csv", help="optional event-level CSV output")
    args = parser.parse_args()
    try:
        def parse(value):
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is None:
                raise ValueError("time must include a timezone offset")
            return int(parsed.timestamp())
        started_at, ends_at = parse(args.started_at), parse(args.ends_at)
        with connect_readonly(args.recordings) as conn:
            conn.execute("BEGIN")
            events = analyse(conn, RPC(args.rpc_url), args.symbol, started_at, ends_at)
        zone = ZoneInfo("Asia/Bangkok")
        stamp = lambda ts: datetime.fromtimestamp(ts, zone).strftime("%m-%d %H:%M:%S")
        print(f"{args.symbol} | mint events {len(events)} | recipients {len({e['recipient'] for e in events})}")
        print("RECIPIENT                                  ROUNDS BUYS  MEDIAN TO END  RANGE       SHARES")
        for row in summarise(events):
            print(f"{row['recipient']} {row['rounds']:6d} {row['buys']:4d} {row['median_seconds_to_end']:8d}s "
                  f"{row['min_seconds_to_end']:4d}..{row['max_seconds_to_end']:4d}s {row['shares']:10.6f}")
        print("\nEVENTS")
        for event in events:
            timing = "ใน window" if event["inside_live_window"] else f"เทียบเปิด window {event['relative_to_window_open_seconds']:+d}s"
            print(f"{stamp(event['timestamp'])} | {event['recipient']} | {event['side']} | "
                  f"เหลือ {event['seconds_to_end']}s | {timing} | {event['shares']:.6f} shares | {event['tx_hash']}")
        if args.csv:
            path = Path(args.csv)
            if path.suffix.lower() != ".csv" or path.is_symlink():
                raise ValueError("CSV destination must be a regular .csv path")
            with path.open("w", newline="") as stream:
                if events:
                    writer = csv.DictWriter(stream, fieldnames=list(events[0]))
                    writer.writeheader(); writer.writerows(events)
    except Exception as exc:
        parser.exit(2, f"Buyer analysis failed ({type(exc).__name__}). Check interval, recordings and RPC.\n")


if __name__ == "__main__":
    main()
