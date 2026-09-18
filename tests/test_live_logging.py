"""Telemetry must be causal, honest about staleness, and independent of trading."""
from dataclasses import replace
import json

from strategy_lab.execution.config import Settings
from strategy_lab.execution.logging import LiveLog, market_status
from strategy_lab.execution.signals import Snapshot
from strategy_lab.replay import RoundRecord

START = 1789443000
NOW = START + 600


def snapshot():
    record = RoundRecord("XYZCL", START, START + 900, 99.75, "",
                         [(NOW - 5, 99.75), (NOW, 99.9), (NOW + 100, 1000)], [])
    return Snapshot(record, "pool", "up", "down", NOW, "missing")


def report(now=NOW, **changes):
    return {"reason": "no fresh Strategy signal", "market": market_status(snapshot(), now, Settings()),
            "positions": [], "transactions": [], "cash_usd": 10., "reserved_usd": 0.,
            "claim_gas_wei": 0, **changes}


def test_market_uses_latest_causal_price_and_first_signal():
    market = report()["market"]
    assert market["price"] == 99.9
    assert abs(market["delta_pct"] - (99.9 - 99.75) / 99.75 * 100) < 1e-10
    assert market["first_signal_timestamp"] == NOW
    assert market["remaining_seconds"] == 300
    assert market["price_stale"] is False


def test_heartbeat_new_tick_reason_and_stale_price_are_visible():
    lines = []
    logger = LiveLog(emit=lines.append)
    logger.report(report(), NOW)
    assert any("ราคา 99.900000" in line and "Strike 99.750000" in line for line in lines)
    count = len(lines)
    logger.report(report(NOW + 2), NOW + 2)
    assert len(lines) == count
    logger.report(report(NOW + 6), NOW + 6)
    assert "พลาดสัญญาณแรก UP มา 6s" in lines[-1]
    logger.report(report(NOW + 7, reason="stale oracle price"), NOW + 7)
    assert "เก่าเกินกำหนด" in lines[-1]
    logger.report(report(NOW + 17), NOW + 17)
    assert "[STALE]" in lines[-1] and "อายุ 17s" in lines[-1]
    updated = replace(snapshot(), record=replace(snapshot().record, prices=[(NOW, 99.9), (NOW + 18, 99.91)]))
    market = market_status(updated, NOW + 18, Settings())
    logger.report(report(NOW + 18, market=market), NOW + 18)
    assert "99.910000" in lines[-1] and "อายุ 0s" in lines[-1]


def test_order_logged_only_when_changed_and_hash_before_finality_is_visible():
    lines = []
    logger = LiveLog(emit=lines.append)
    position = {"id": "position-one", "state": "BUY_PENDING", "symbol": "XYZCL", "side": "UP",
                "amount": 1000000, "cost": 0, "shares": 0, "payout": 0, "error": None}
    data = report(positions=[position], transactions=[{"position_id": "position-one", "operation": "buy", "tx_hash": "0x123"}])
    logger.report(data, NOW)
    assert any("https://arbiscan.io/tx/0x123" in line for line in lines)
    assert any("ไม่ใช่ยอด wallet สด" in line for line in lines)
    count = sum("ORDER" in line for line in lines)
    logger.report(data, NOW + 10)
    assert sum("ORDER" in line for line in lines) == count
    position["state"] = "OPEN"
    position["shares"] = 1314422
    position["cost"] = 1000000
    logger.report(data, NOW + 11)
    assert sum("ORDER" in line for line in lines) == count + 1


def test_json_logs_include_price_and_human_logs_no_invented_price():
    lines = []
    logger = LiveLog(format="json", emit=lines.append)
    logger.report(report(), NOW)
    assert json.loads(lines[-1])["market"]["price"] == 99.9
    logger = LiveLog(emit=lines.append)
    logger.report(report(market=None, reason="recordings unavailable"), NOW)
    assert "ราคา --" in lines[-1]
    missing = replace(snapshot(), record=replace(snapshot().record, prices=[]))
    status = market_status(missing, NOW, Settings())
    assert status["price"] is None and status["delta_pct"] is None and status["price_stale"] is True


def test_outside_window_log_uses_configured_live_window():
    settings = Settings()
    object.__setattr__(settings, "trade_window_open_seconds", 540)
    object.__setattr__(settings, "trade_window_close_seconds", 75)
    data = report(reason="outside Trade Window", market=market_status(snapshot(), NOW, settings))
    lines = []
    LiveLog(emit=lines.append).report(data, NOW)
    assert "live เหลือ 540–75s" in lines[-1]
