"""Human-readable live telemetry from recorded prices, without extra RPC or secrets."""
from datetime import datetime
from dataclasses import replace
import json
import math
from zoneinfo import ZoneInfo

from ..replay import DELTA_THRESHOLD_PCT, DEFAULT_DELTA_THRESHOLD_PCT, delta_edge


def clock(ts, timezone):
    return datetime.fromtimestamp(ts, timezone).strftime("%H:%M:%S")


def market_status(snapshot, now, settings):
    if snapshot is None:
        return None
    record = snapshot.record
    prices = [(ts, value) for ts, value in record.prices if record.starting <= ts <= now]
    latest = max(prices, key=lambda pair: pair[0]) if prices else None
    price = latest[1] if latest and math.isfinite(latest[1]) and latest[1] > 0 else None
    strike = record.strike if math.isfinite(record.strike) and record.strike > 0 else None
    delta = (price - strike) / strike * 100 if price is not None and strike else None
    # Use the same first-crossing rule as the strategy, but never change its decision.
    window_opens = getattr(settings, "trade_window_open_seconds", 300)
    window_closes = getattr(settings, "trade_window_close_seconds", 75)
    first = (delta_edge(window_opens=window_opens, window_closes=window_closes)
             .decide(replace(record, prices=prices))) if prices and strike else None
    return {"symbol": record.symbol, "round_start": record.starting, "round_end": record.ending,
            "pool": snapshot.pool, "price": price, "strike": strike, "delta_pct": delta,
            "threshold_pct": DELTA_THRESHOLD_PCT.get(record.symbol, DEFAULT_DELTA_THRESHOLD_PCT),
            "price_timestamp": latest[0] if latest else None,
            "price_age_seconds": now - latest[0] if latest else None,
            "price_stale": not latest or now - latest[0] > settings.max_age_seconds,
            "remaining_seconds": max(0, record.ending - now),
            "first_signal_timestamp": first.at if first else None,
            "first_signal_side": first.side if first else None,
            "signal_age_seconds": now - first.at if first else None,
            "max_signal_age_seconds": settings.max_signal_age_seconds,
            "trade_window_open_seconds": window_opens,
            "trade_window_close_seconds": window_closes}


def explain(reason, market):
    if reason == "no fresh Strategy signal":
        if market and market["signal_age_seconds"] is not None and market["signal_age_seconds"] > market["max_signal_age_seconds"]:
            return f"พลาดสัญญาณแรก {market['first_signal_side']} มา {market['signal_age_seconds']}s แล้ว — รอรอบถัดไป"
        return "รอ Delta เข้าเงื่อนไข"
    outside = "อยู่นอกช่วงซื้อ"
    if market:
        outside += (f" (live เหลือ {market['trade_window_open_seconds']}–"
                    f"{market['trade_window_close_seconds']}s)")
    return {
        "outside Trade Window": outside,
        "new entries disabled": "ปิดซื้อใหม่ / มีไฟล์ HALT",
        "configured lifetime trade count reached": "ครบจำนวนรอบทดลอง — ยังติดตามสถานะ/claim ต่อ",
        "overnight entry deadline reached": "ครบเวลาค้างคืน — หยุดซื้อใหม่ แต่ยังติดตาม/claim ต่อ",
        "overnight loss limit reached": "ถึงขีดจำกัดขาดทุนสะสม 2 USDC — ยังติดตาม/claim ต่อ",
        "already recorded": "รอบนี้บันทึกคำสั่งแล้ว — ไม่ส่งซื้อซ้ำ",
        "no live Round recorded": "รอข้อมูลรอบจาก Collector",
        "recordings unavailable": "อ่านข้อมูล Collector ไม่ได้ — ไม่ส่งซื้อใหม่",
        "stale oracle price": "ราคาจาก Collector เก่าเกินกำหนด",
        "stale Round metadata": "ข้อมูลรอบจาก Collector เก่าเกินกำหนด",
        "Partial Round at decision time": "ข้อมูลราคาของรอบไม่ครบ",
        "Oracle Stale at decision time": "ราคาของรอบยังไม่มีการเปลี่ยนแปลง",
        "signal expired while fetching live quote": "สัญญาณหมดอายุระหว่างอ่าน quote — ไม่ส่งซื้อ",
        "BUY_UNKNOWN": "ข้ามรอบนี้ — API ไม่ยืนยันการซื้อ กันเงินไว้และตรวจเชนต่อ ไม่ส่งซ้ำ",
        "unknown buy exposure limit reached": "รายการซื้อไม่ทราบผลรวมถึง 2 USDC — หยุดซื้อเพิ่มเพื่อตรวจสอบ",
        "BUY_PENDING": "ส่งซื้อแล้ว/รอตรวจสอบผล — ยังไม่ยืนยันว่าได้ shares",
        "open-position limit": "มีออร์เดอร์ค้าง — รอยืนยันซื้อ/ผลรอบ/claim ก่อนซื้อเพิ่ม",
        "waiting for buy confirmation or claim": "รอยืนยันซื้อหรือเคลมก่อน — ยังไม่เปิดออร์เดอร์ซ้อน",
        "quote refused: live entries halted": "หยุดซื้อชั่วคราว — มี halt flag ค้างอยู่",
        "quote refused: insufficient USDC or wrong stake":
            "USDC ในกระเป๋าไม่พอสำหรับไม้ละ 1 USDC",
        "quote refused: fund claim gas before buying":
            "ETH ไม่พอจ่าย gas ตอนเคลม — เติมก่อนถึงจะซื้อได้",
        "quote refused: Accounts mint has no verified minimum output; acknowledge in config":
            "ต้องตั้ง accept_unprotected_slippage เป็น true ใน config ก่อน",
        "quote refused: too near buy cutoff": "ใกล้เวลาปิดซื้อเกินไป (ต้องเหลือเกิน 75s)",
        "quote refused: pool already has shares; use a dedicated wallet":
            "กระเป๋านี้ถือ shares ของ pool นี้อยู่แล้ว — ต้องใช้กระเป๋าเฉพาะ",
        "quote refused: pool expiry does not match Round": "เวลาปิดของ pool ไม่ตรงกับรอบ",
        "quote refused: pool outcomes do not match Round": "outcome ของ pool ไม่ตรงกับรอบ",
    }.get(reason, reason)


class LiveLog:
    def __init__(self, format="text", timezone="Asia/Bangkok", interval=5, emit=None):
        self.format, self.timezone = format, ZoneInfo(timezone)
        self.interval = interval
        self.emit = emit or (lambda text: print(text, flush=True))
        self.last_key = None
        self.last_at = None
        self.positions = {}
        self.round = None
        self.session_key = None
        self.floor = None

    def preflight(self, result, execute):
        if self.format == "json":
            self.emit(json.dumps({"preflight": result, "execute": execute}))
            return
        self.emit(f"PRECHECK | wallet={result['wallet']} | Arbitrum {result['chain_id']}")
        self.emit(f"WALLET   | USDC={result['usdc_micro']/1e6:.6f} | ETH={result['eth_wei']/1e18:.8f} | signer ตรงกับ wallet | API auth ยังไม่ยืนยัน")
        self.emit(f"MODE     | {'LIVE: ส่งเงินจริงได้ตาม config' if execute else 'CHECK ONLY: ไม่ส่งธุรกรรม'} | เวลา {self.timezone.key} | ราคา=9lives oracle ผ่าน Collector (ไม่ใช่ราคา shares)")

    def report(self, report, now):
        market = report.get("market")
        # New tick/status prints immediately; otherwise provide a bounded heartbeat.
        key = json.dumps({"reason": report["reason"], "positions": report["positions"],
                          "transactions": report.get("transactions", []),
                          "tick": (market["round_end"], market["price_timestamp"], market["price"], market["strike"]) if market else None}, sort_keys=True)
        if key == self.last_key and self.last_at is not None and now - self.last_at < self.interval:
            return
        self.last_key, self.last_at = key, now
        if self.format == "json":
            self.emit(json.dumps({"logged_at": now, **report}))
            return
        prefix = f"[{clock(now, self.timezone)}]"
        floor = report.get("min_quote_shares_micro")
        if floor and self.floor != floor:
            self.emit(f"{prefix} BUY FILTER | 1 USDC ต้องได้ quote > {floor/1e6:.6f} shares (เท่ากันก็ข้าม) | API ไม่รับประกันขั้นต่ำตอน fill")
            self.floor = floor
        session = report.get("overnight")
        session_key = json.dumps(session, sort_keys=True)
        if session and self.session_key != session_key:
            end = datetime.fromtimestamp(session["ends_at"], self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            mode = "CONTINUOUS" if session.get("continuous") else "OVERNIGHT"
            stop = "ไม่กำหนดเวลาหยุด — ใช้ HALT หยุดซื้อใหม่" if session.get("continuous") else f"หยุดซื้อ {end}"
            self.emit(f"{prefix} {mode} | {stop} | ครั้ง {session['attempts']} (ไม่จำกัด) | ซื้อสะสม {session['committed_micro']/1e6:.2f} USDC | ทุนตั้งต้น 10 USDC หมุนเงิน claim ยืนยันแล้ว | ขาดทุน {session['loss_micro']/1e6:.2f}/2.00 | หลังครบเวลายัง claim ต่อ")
            self.session_key = session_key
        if market:
            if self.round != market["round_end"]:
                self.emit(f"{prefix} ROUND {market['symbol']} | {clock(market['round_start'], self.timezone)}–{clock(market['round_end'], self.timezone)} | เกณฑ์ |Delta| > {market['threshold_pct']:.2f}%")
                self.round = market["round_end"]
            price = f"{market['price']:,.6f}" if market["price"] is not None else "--"
            strike = f"{market['strike']:,.6f}" if market["strike"] is not None else "--"
            delta = f"{market['delta_pct']:+.4f}%" if market["delta_pct"] is not None else "--"
            feed = clock(market["price_timestamp"], self.timezone) if market["price_timestamp"] is not None else "--"
            age = f"{market['price_age_seconds']}s" if market["price_age_seconds"] is not None else "--"
            minutes, seconds = divmod(market["remaining_seconds"], 60)
            self.emit(f"{prefix} {market['symbol']} | ราคา {price} | Strike {strike} | Delta {delta} | เหลือ {minutes:02d}:{seconds:02d} | feed {feed} อายุ {age}{' [STALE]' if market['price_stale'] else ''} | {explain(report['reason'], market)}")
        else:
            self.emit(f"{prefix} WAIT | ราคา -- | {explain(report['reason'], None)}")
        txs = report.get("transactions", [])
        for p in report["positions"]:
            refs = [(t["operation"], t["tx_hash"]) for t in txs if t["position_id"] == p["id"] and t["tx_hash"]]
            state_key = json.dumps([p, refs], sort_keys=True)
            if self.positions.get(p["id"]) == state_key:
                continue
            self.positions[p["id"]] = state_key
            self.emit(f"{prefix} ORDER {p['state']} | {p['symbol']} {p['side']} | id={p['id']} | stake={p['amount']/1e6:.2f} USDC | จ่ายยืนยัน={p['cost']/1e6:.6f} | shares={p['shares']/1e6:.6f} | รับคืนยืนยัน={p['payout']/1e6:.6f}")
            for operation, tx in refs:
                self.emit(f"{prefix} TX {operation} | https://arbiscan.io/tx/{tx}")
            if p.get("error"):
                self.emit(f"{prefix} REVIEW | {p['error']}")
            self.emit(f"{prefix} LEDGER | ยอดคำนวณ={report['cash_usd']:.6f} USDC (ไม่ใช่ยอด wallet สด) | กันไว้={report['reserved_usd']:.2f} | claim gas={report['claim_gas_wei']/1e18:.8f} ETH")
