"""Read-only overnight comparison against the existing dashboard's Delta Edge replay."""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ..db import connect_readonly
from ..replay import DELTA_EDGE, replay


def compare(live, recordings, symbol):
    session = live.execute("SELECT * FROM overnight_session WHERE id=1").fetchone()
    if not session:
        raise ValueError("no saved overnight session")
    baseline = set(json.loads(session["baseline_ids"]))
    live_positions = {p["ending"]: dict(p) for p in live.execute("SELECT * FROM positions WHERE symbol=?", (symbol,))
                      if p["id"] not in baseline and session["started_at"] <= p["created_at"] < session["ends_at"]}
    # Exactly the original dashboard model, with its default quality filters. No live
    # share floor, gas, finality delay or one-position limit is added to that baseline.
    paper = replay(recordings, symbol, strategies=[DELTA_EDGE])["Delta Edge"]
    paper_trades = {p.round_ending: p for p in paper.trades
                    if session["started_at"] <= p.entered_at < session["ends_at"]}
    rows = []
    for ending in sorted(set(live_positions) | set(paper_trades)):
        l, p = live_positions.get(ending), paper_trades.get(ending)
        closed = bool(l and l["state"] in ("REDEEMED", "LOST"))
        live_pnl = (l["payout"] - l["cost"]) / 1e6 if closed else None
        rows.append({"round_ending": ending,
                     "live_side": l["side"] if l else None, "live_state": l["state"] if l else None,
                     "live_entry_at": l["created_at"] if l else None,
                     "live_quoted_shares": l["quoted_shares"] / 1e6 if l else None,
                     "live_shares": l["shares"] / 1e6 if l and l["shares"] else None,
                     "live_cost_usdc": l["cost"] / 1e6 if l else None,
                     "live_payout_usdc": l["payout"] / 1e6 if l else None,
                     "live_realized_pnl_usdc": live_pnl,
                     "paper_side": p.side if p else None, "paper_entry_at": p.entered_at if p else None,
                     "paper_shares": p.shares / 1e6 if p else None,
                     "paper_pnl_usdc": p.pnl if p else None,
                     "paper_model_above_1_30": p.shares > 1_300_000 if p else None,
                     "same_side": l["side"] == p.side if l and p else None})
    ids = {p["id"] for p in live_positions.values()}
    gas = sum(row["gas_wei"] or 0 for row in live.execute(
        "SELECT position_id,gas_wei FROM live_ops WHERE operation='redeem'") if row["position_id"] in ids)
    return {"symbol": symbol, "started_at": session["started_at"], "ends_at": session["ends_at"],
            "live_attempts": len(live_positions),
            "live_closed": sum(p["state"] in ("REDEEMED", "LOST") for p in live_positions.values()),
            "live_realized_pnl_usdc": sum(r["live_realized_pnl_usdc"] or 0 for r in rows),
            "confirmed_claim_gas_eth": gas / 1e18, "paper_trades": len(paper_trades),
            "paper_pnl_usdc": sum(p.pnl for p in paper_trades.values()), "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("BTC", "XYZCL"), default="XYZCL")
    parser.add_argument("--ledger")
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--csv", help="optional .csv output; never a database path")
    args = parser.parse_args()
    try:
        with connect_readonly(args.ledger or f"data/live-{args.symbol}.db") as live, connect_readonly(args.recordings) as data:
            live.execute("BEGIN")
            data.execute("BEGIN")
            result = compare(live, data, args.symbol)
        zone = ZoneInfo("Asia/Bangkok")
        stamp = lambda ts: datetime.fromtimestamp(ts, zone).strftime("%m-%d %H:%M")
        print(f"{args.symbol} | {stamp(result['started_at'])} → {stamp(result['ends_at'])} เวลาไทย")
        print(f"LIVE: {result['live_attempts']} intents / ปิดแล้ว {result['live_closed']} | PnL ยืนยัน {result['live_realized_pnl_usdc']:+.6f} USDC | claim gas ยืนยัน {result['confirmed_claim_gas_eth']:.8f} ETH")
        print(f"PAPER dashboard: {result['paper_trades']} trades | PnL {result['paper_pnl_usdc']:+.6f} USDC")
        print("ROUND         LIVE                 SHARES      PNL USDC  | PAPER   SHARES      PNL USDC")
        fmt = lambda v: "--" if v is None else f"{v:.6f}"
        for r in result["rows"]:
            label = f"{r['live_side'] or '--'} {r['live_state'] or '--'}"
            print(f"{stamp(r['round_ending'])}  {label:20} {fmt(r['live_shares']):>10} {fmt(r['live_realized_pnl_usdc']):>10} | {r['paper_side'] or '--':5} {fmt(r['paper_shares']):>10} {fmt(r['paper_pnl_usdc']):>10}")
        print("Paper คือแบบเดิม ไม่มี filter >1.30 / gas / finality / จำกัดเปิดครั้งละหนึ่งรายการ; -- หมายถึงไม่มีข้อมูลเทียบหรือยังไม่ปิด ไม่ใช่ขาดทุน")
        if args.csv:
            path = Path(args.csv)
            protected = [Path(args.ledger or f"data/live-{args.symbol}.db"), Path(args.recordings)]
            if path.suffix.lower() != ".csv" or path.is_symlink() or any(
                    path.resolve() == p.resolve() or (path.exists() and p.exists() and path.samefile(p)) for p in protected):
                raise ValueError("unsafe report destination")
            with path.open("w", newline="") as stream:
                if result["rows"]:
                    writer = csv.DictWriter(stream, fieldnames=list(result["rows"][0]))
                    writer.writeheader()
                    writer.writerows(result["rows"])
    except Exception as exc:
        parser.exit(2, f"Comparison failed ({type(exc).__name__}). Check ledger/session/recordings paths.\n")


if __name__ == "__main__":
    main()
