"""Commands over the live ledgers, sent from a phone.

The point of the confirmation is not politeness. A halt fires when a guard caught
something, and a loss limit fires when money was lost, so the commands that undo them
have to put the reason in front of a person before they will act.
"""
import json
import pathlib
import sqlite3
import time

import pytest

from strategy_lab.execution.notify import (Control, alerts, clear_halt, fills, reset_loss,
                                           status, tail)

NOW = int(time.time())


def a_ledger(tmp_path, name="live-BTC.db", halt=None, continuous=True, positions=()):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE live_flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
           CREATE TABLE overnight_session (id INTEGER PRIMARY KEY, started_at INTEGER,
               ends_at INTEGER, baseline_ids TEXT NOT NULL);
           CREATE TABLE positions (id TEXT PRIMARY KEY, side TEXT, state TEXT,
               ending INTEGER, cost INTEGER, payout INTEGER, shares INTEGER,
               quoted_shares INTEGER, minimum_shares INTEGER, updated_at INTEGER);""")
    if halt:
        conn.execute("INSERT INTO live_flags VALUES ('halt', ?)", (halt,))
    if continuous:
        conn.execute("INSERT INTO live_flags VALUES ('continuous', '1')")
    conn.execute("INSERT INTO overnight_session VALUES (1, ?, ?, ?)", (NOW, NOW, json.dumps([])))
    for row in positions:
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()
    return str(path)


def a_position(identity, state, side="UP", cost=1_000_000, payout=1_314_422,
               shares=1_314_422, quoted=1_314_422, minimum=1_301_178):
    return (identity, side, state, NOW - 100, cost, payout, shares, quoted, minimum, NOW)


def recordings(tmp_path, ages=(("BTC", 2),), name="lab.db"):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS oracle_prices"
                 " (symbol TEXT, ts INTEGER, price REAL)")
    conn.execute("DELETE FROM oracle_prices")
    for symbol, age in ages:
        conn.execute("INSERT INTO oracle_prices VALUES (?,?,?)", (symbol, NOW - age, 100.0))
    conn.commit()
    conn.close()
    return str(path)


class TestClearingAHalt:
    def test_it_will_not_act_without_being_asked_twice(self, tmp_path):
        ledger = a_ledger(tmp_path, halt="actual fill below quoted minimum")
        control = Control({"BTC": ledger}, recordings(tmp_path))

        answer = control.handle("/clearhalt BTC")

        assert "/clearhalt BTC yes" in answer
        assert sqlite3.connect(ledger).execute(
            "SELECT COUNT(*) FROM live_flags WHERE name='halt'").fetchone()[0] == 1

    def test_the_first_answer_shows_why_it_stopped(self, tmp_path):
        ledger = a_ledger(tmp_path, halt="actual fill below quoted minimum",
                          positions=[a_position("a" * 64, "REDEEMED", shares=1_050_199)])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        answer = control.handle("/clearhalt BTC")

        assert "1.050199" in answer and "95.2%" in answer

    def test_confirming_clears_it(self, tmp_path):
        ledger = a_ledger(tmp_path, halt="actual fill below quoted minimum")
        control = Control({"BTC": ledger}, recordings(tmp_path))

        assert "cleared" in control.handle("/clearhalt BTC yes")
        assert sqlite3.connect(ledger).execute(
            "SELECT COUNT(*) FROM live_flags WHERE name='halt'").fetchone()[0] == 0

    def test_clearing_nothing_says_so(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path)}, recordings(tmp_path))
        assert "no halt" in control.handle("/clearhalt BTC yes")


class TestResettingTheLossLimit:
    def test_it_refuses_while_money_is_still_committed(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[a_position("b" * 64, "OPEN", payout=0)])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        answer = control.handle("/resetloss BTC yes")

        assert "refused" in answer and "still open" in answer

    def test_it_names_what_is_holding_it(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[a_position("b" * 64, "REDEEM_PENDING", payout=0)])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        assert "REDEEM_PENDING" in control.handle("/resetloss BTC yes")

    def test_it_resets_once_everything_is_settled(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[a_position("b" * 64, "REDEEMED")])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        assert "reset" in control.handle("/resetloss BTC yes")
        baseline = json.loads(sqlite3.connect(ledger).execute(
            "SELECT baseline_ids FROM overnight_session").fetchone()[0])
        assert "b" * 64 in baseline   # history kept, counter starts from here

    def test_it_asks_twice_as_well(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[a_position("b" * 64, "REDEEMED")])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        assert "/resetloss BTC yes" in control.handle("/resetloss BTC")
        assert json.loads(sqlite3.connect(ledger).execute(
            "SELECT baseline_ids FROM overnight_session").fetchone()[0]) == []


class TestStatus:
    def test_it_reports_every_market_at_once(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path), "XYZCL": a_ledger(tmp_path, "x.db")},
                          recordings(tmp_path, [("BTC", 2), ("XYZCL", 3)]))

        answer = control.handle("/status")

        assert "BTC" in answer and "XYZCL" in answer

    def test_a_stale_feed_is_marked(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path)},
                          recordings(tmp_path, [("BTC", 900)]))

        assert "STALE" in control.handle("/status")

    def test_an_open_position_says_how_long_the_round_has(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[a_position("c" * 64, "OPEN", payout=0)])
        control = Control({"BTC": ledger}, recordings(tmp_path))

        assert "ago" in control.handle("/status")


class TestRefusingTheUnknown:
    @pytest.mark.parametrize("text", ["/clearhalt", "/fills", "/resetloss"])
    def test_a_command_with_no_market_lists_them(self, tmp_path, text):
        control = Control({"BTC": a_ledger(tmp_path)}, recordings(tmp_path))
        assert "name a market" in control.handle(text)

    def test_an_unknown_market_is_refused(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path)}, recordings(tmp_path))
        assert "unknown market" in control.handle("/clearhalt DOGE yes")

    def test_anything_unrecognised_gets_the_help(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path)}, recordings(tmp_path))
        assert "/status" in control.handle("/wat")


class TestAlerts:
    def test_a_new_halt_is_announced_once(self, tmp_path):
        ledger = a_ledger(tmp_path, halt="actual fill below quoted minimum")
        control = Control({"BTC": ledger}, recordings(tmp_path))

        first, state = alerts(control, {})
        again, _ = alerts(control, state)

        assert len(first) == 1 and "stopped trading" in first[0]
        assert again == []

    def test_a_feed_going_quiet_and_coming_back_are_both_said(self, tmp_path):
        ledger = a_ledger(tmp_path)
        quiet = Control({"BTC": ledger}, recordings(tmp_path, [("BTC", 900)]))
        messages, state = alerts(quiet, {})
        assert "stale" in messages[0]

        back = Control({"BTC": ledger}, recordings(tmp_path, [("BTC", 2)], "fresh.db"))
        messages, _ = alerts(back, state)
        assert "back" in messages[0]


class TestReadingTheLog:
    def test_it_returns_the_last_lines(self, tmp_path):
        path = tmp_path / "run.log"
        path.write_text("\n".join(f"line {n}" for n in range(200)))
        assert "line 199" in tail(str(path)) and "line 0" not in tail(str(path))

    def test_a_missing_log_does_not_raise(self, tmp_path):
        assert "cannot read" in tail(str(tmp_path / "nope.log"))


class TestButtons:
    """Everything reachable by tapping, without weakening the two steps a destructive
    command takes."""

    def a_control(self, tmp_path, **kwargs):
        return Control({"BTC": a_ledger(tmp_path, **kwargs),
                        "XYZCL": a_ledger(tmp_path, "x.db")}, recordings(tmp_path))

    def test_the_menu_offers_every_market(self, tmp_path):
        labels = [b["text"] for row in self.a_control(tmp_path).menu() for b in row]
        assert any("BTC: fills" in l for l in labels)
        assert any("XYZCL: fills" in l for l in labels)

    def test_a_button_press_is_the_same_command_as_typing_it(self, tmp_path):
        control = self.a_control(tmp_path, halt="fill below minimum")
        presses = [b["callback_data"] for row in control.menu() for b in row]

        assert "clearhalt BTC" in presses
        # And it routes exactly as the typed form does.
        assert "/clearhalt BTC yes" in control.handle("clearhalt BTC")

    def test_the_menu_never_offers_a_one_tap_destruction(self, tmp_path):
        presses = [b["callback_data"] for row in self.a_control(tmp_path).menu() for b in row]
        assert not any(p.endswith(" yes") for p in presses)

    def test_asking_to_resume_offers_the_confirming_button(self, tmp_path):
        control = self.a_control(tmp_path, halt="fill below minimum")
        buttons = control.buttons_for("clearhalt BTC")
        presses = [b["callback_data"] for row in buttons for b in row]

        assert "clearhalt BTC yes" in presses
        assert "status" in presses          # and a way out

    def test_the_confirming_press_falls_back_to_the_menu(self, tmp_path):
        control = self.a_control(tmp_path, halt="fill below minimum")
        presses = [b["callback_data"] for row in control.buttons_for("clearhalt BTC yes")
                   for b in row]
        assert "clearhalt BTC yes" not in presses

    def test_an_unknown_market_gets_the_plain_menu(self, tmp_path):
        control = self.a_control(tmp_path)
        assert control.buttons_for("clearhalt DOGE") == control.menu()


SAMPLE_LOG = """\
[14:20:12] ORDER OPEN | BTC UP | id=aaa | stake=1.00 USDC | จ่ายยืนยัน=1.000000 | shares=1.314422 | รับคืนยืนยัน=0.000000
[14:30:40] ORDER REDEEMED | BTC UP | id=aaa | stake=1.00 USDC | จ่ายยืนยัน=1.000000 | shares=1.314422 | รับคืนยืนยัน=1.314422
[14:31:02] REVIEW | BUY_HASH_UNKNOWN_TO_CHAIN: skipped Round; funds reserved
[14:33:07] BTC | ราคา 84,515.500000 | Strike 84,377.500000 | Delta +0.1636% | เหลือ 11:53 | feed 14:33:05 อายุ 2s | อยู่นอกช่วงซื้อ
[14:33:25] BTC | ราคา 84,461.500000 | Strike 84,377.500000 | Delta +0.0996% | เหลือ 11:35 | feed 14:33:24 อายุ 1s | รอ Delta เข้าเงื่อนไข
"""


class TestReadingTheLogOnAPhone:
    """A heartbeat repeats every few seconds and wraps to six lines on a phone. Only the
    last one is the state; the events between them are what there is to read."""

    def test_only_the_newest_heartbeat_survives(self):
        from strategy_lab.execution.notify import format_log
        answer = format_log(SAMPLE_LOG)

        assert "+0.0996%" in answer          # the latest
        assert "+0.1636%" not in answer      # and not the one before it

    def test_it_keeps_the_state_worth_seeing(self):
        from strategy_lab.execution.notify import format_log
        answer = format_log(SAMPLE_LOG)

        assert "11:35 left" in answer and "feed 1s" in answer
        assert "รอ Delta เข้าเงื่อนไข" in answer

    def test_trailing_zeros_are_dropped(self):
        from strategy_lab.execution.notify import format_log
        answer = format_log(SAMPLE_LOG)

        assert "84,461.5 vs 84,377.5" in answer
        assert "84,461.500000" not in answer

    def test_events_are_kept_and_marked(self):
        from strategy_lab.execution.notify import format_log
        answer = format_log(SAMPLE_LOG)

        assert "REDEEMED UP +0.314" in answer
        assert "OPEN UP" in answer
        assert "BUY_HASH_UNKNOWN_TO_CHAIN" in answer

    def test_it_is_short_enough_to_read(self):
        from strategy_lab.execution.notify import format_log
        lines = format_log(SAMPLE_LOG).splitlines()

        assert len(lines) <= 14
        assert max(len(line) for line in lines) <= 70

    def test_an_unrecognised_log_falls_back_to_its_own_words(self):
        from strategy_lab.execution.notify import format_log
        answer = format_log("something nobody has seen before\nand another line")

        assert "nobody has seen" in answer

    def test_an_empty_log_says_so(self):
        from strategy_lab.execution.notify import format_log
        assert format_log("") == "log is empty"



class TestFittingOnAPhone:
    """Every answer has to be readable in a narrow column without horizontal scrolling."""

    def _wide(self, text):
        return max(len(line) for line in text.splitlines())

    def test_status_stays_narrow(self, tmp_path):
        control = Control({"BTC": a_ledger(tmp_path), "XYZCL": a_ledger(tmp_path, "x.db")},
                          recordings(tmp_path, [("BTC", 2), ("XYZCL", 3)]))
        assert self._wide(control.handle("/status")) <= 48

    def test_fills_stays_narrow_and_leads_with_the_common_case(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[
            a_position("a" * 64, "REDEEMED"),
            a_position("b" * 64, "REDEEMED"),
            a_position("c" * 64, "REDEEMED", shares=1_050_199, payout=1_050_199),
        ])
        control = Control({"BTC": ledger}, recordings(tmp_path))
        answer = control.handle("/fills BTC")
        rows = [line for line in answer.splitlines() if "needs" in line]

        assert self._wide(answer) <= 48
        assert "1.314422" in rows[0]          # the usual price first
        assert rows[-1].endswith("⚠️")        # the exception marked, last
        assert "break-even" in answer

    def test_fills_summarises_the_share_below_quote(self, tmp_path):
        ledger = a_ledger(tmp_path, positions=[
            a_position("a" * 64, "REDEEMED"),
            a_position("c" * 64, "REDEEMED", shares=1_050_199, payout=1_050_199),
        ])
        control = Control({"BTC": ledger}, recordings(tmp_path))
        assert "below quote: 1 (50.0%)" in control.handle("/fills BTC")

    def test_nothing_is_sent_as_markdown(self):
        """Machine-made text cannot be trusted to balance markup, and Telegram drops the
        whole message when it does not."""
        source = pathlib.Path("strategy_lab/execution/notify.py").read_text()
        assert '"parse_mode"' not in source
