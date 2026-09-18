import sqlite3

from strategy_lab.db import initialise
from strategy_lab.execution.buyers import analyse, summarise


POOL = "0x" + "11" * 20
UP_SHARE = "0x" + "22" * 20
DOWN_SHARE = "0x" + "33" * 20
BUYER = "0x" + "44" * 20
RELAYER = "0x" + "55" * 20
TX = "0x" + "66" * 32


class FakeRPC:
    def view(self, pool, signature, inputs=(), values=(), outputs=(), block="latest"):
        return (UP_SHARE if values[0].hex() == "0102030405060708" else DOWN_SHARE,)

    def call(self, method, params):
        if method == "eth_blockNumber": return hex(100)
        if method == "eth_getBlockByNumber":
            number = 100 if params[0] == "latest" else int(params[0], 16)
            return {"timestamp": hex(900 + number * 10)}
        if method == "eth_getLogs":
            return [{"address": UP_SHARE, "removed": False, "blockNumber": hex(15),
                     "transactionHash": TX, "data": hex(1_314_422),
                     "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                "0x" + "0" * 64, "0x" + "0" * 24 + BUYER[2:]]}]
        if method == "eth_getTransactionByHash": return {"from": RELAYER}
        raise AssertionError((method, params))


def test_groups_repeat_recipients_and_times_relative_to_trade_window():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialise(conn)
    conn.execute("""INSERT INTO rounds
        (symbol,ending,starting,pool_address,outcome_up,outcome_down,first_seen_at,last_seen_at,source,code_version)
        VALUES ('XYZCL',1800,900,?,'0x0102030405060708','0x1112131415161718',900,900,'live','test')""", (POOL,))
    events = analyse(conn, FakeRPC(), "XYZCL", 1000, 1800)
    assert len(events) == 1
    assert events[0]["recipient"] == BUYER
    assert events[0]["tx_from"] == RELAYER
    assert events[0]["seconds_to_end"] == 750
    assert events[0]["relative_to_window_open_seconds"] == -450
    assert events[0]["inside_live_window"] is False
    assert summarise(events)[0]["rounds"] == 1
