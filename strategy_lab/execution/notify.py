"""Watch and steer the live runners from Telegram, without opening a way in.

A control panel for a bot that spends real money does not belong on a public URL. This
dials out to Telegram and nothing listens, so the host still accepts no inbound
connection, and who may send a command is decided by a chat id rather than by whatever
reaches the page.

It never loads a private key. Clearing a halt and resetting a risk session are both
writes to the ledger, so this reads and writes ledgers and nothing else — a stolen token
cannot sign a transaction, only reopen trading that a guard stopped.

Which is still worth stopping twice. Every destructive command prints what actually
happened — the Fills, the loss, the open positions — and does nothing until it is sent
again with `yes`, because the guard exists precisely for the moments when a person would
rather not read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org"
TERMINAL = ("REDEEMED", "LOST", "BUY_REJECTED", "EXPIRED")
# Long polling: one outbound request that the server holds open. No webhook, no port.
POLL_TIMEOUT = 50
# The feed publishes about every five seconds; five minutes of nothing is a fault.
FEED_SILENCE_SECONDS = 300


def _open(path: str, writable: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}" + ("" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _flags(conn) -> dict:
    return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM live_flags")}


def _active(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM positions WHERE state NOT IN (?,?,?,?)", TERMINAL)]


def _session(conn):
    """Profit and loss since the running risk session's baseline, as the runner counts it."""
    row = conn.execute("SELECT baseline_ids FROM overnight_session WHERE id=1").fetchone()
    if row is None:
        return None
    baseline = set(json.loads(row["baseline_ids"]))
    rows = [r for r in conn.execute("SELECT * FROM positions") if r["id"] not in baseline]
    done = [r for r in rows if r["state"] in ("LOST", "REDEEMED")]
    return {
        "settled": len(done),
        "wins": sum(1 for r in done if r["payout"] > r["cost"]),
        "gross_loss": sum(max(0, r["cost"] - r["payout"]) for r in done) / 1e6,
        "net": sum(r["payout"] - r["cost"] for r in done) / 1e6,
    }


def feed_age(recordings: str, symbol: str):
    try:
        conn = _open(recordings)
        row = conn.execute("SELECT MAX(ts) t FROM oracle_prices WHERE symbol = ?",
                           (symbol,)).fetchone()
        conn.close()
        return None if row is None or row["t"] is None else int(time.time()) - row["t"]
    except sqlite3.Error:
        return None


def status(symbol: str, ledger: str, recordings: str) -> str:
    conn = _open(ledger)
    try:
        flags, active, session = _flags(conn), _active(conn), _session(conn)
    finally:
        conn.close()
    age = feed_age(recordings, symbol)
    lines = [f"*{symbol}*"]
    if age is None:
        lines.append("  feed: unknown")
    else:
        lines.append(f"  feed: {age}s old" + ("  ⚠️ STALE" if age > FEED_SILENCE_SECONDS else ""))
    lines.append("  halt: " + (f"⛔ {flags['halt']}" if "halt" in flags else "none"))
    if session:
        lines.append(f"  session: {session['wins']}/{session['settled']} won"
                     f" | net {session['net']:+.4f} | loss counted {session['gross_loss']:.2f}/2.00")
    lines.append(f"  open positions: {len(active)}")
    for position in active:
        left = position["ending"] - int(time.time())
        when = f"ends in {left}s" if left > 0 else f"ended {-left}s ago"
        lines.append(f"    {position['id'][:12]} {position['side']} {position['state']} ({when})")
    return "\n".join(lines)


def fills(symbol: str, ledger: str) -> str:
    conn = _open(ledger)
    try:
        short = conn.execute(
            "SELECT id, quoted_shares, shares FROM positions"
            " WHERE shares > 0 AND shares < minimum_shares ORDER BY updated_at").fetchall()
        every = conn.execute("SELECT shares, COUNT(*) n FROM positions WHERE shares > 0"
                             " GROUP BY shares ORDER BY shares").fetchall()
    finally:
        conn.close()
    lines = [f"*{symbol} fills*"]
    for row in short:
        got, quoted = row["shares"] / 1e6, row["quoted_shares"] / 1e6
        lines.append(f"  ⚠️ {row['id'][:12]} quoted {quoted:.6f} got {got:.6f}"
                     f" ({(got / quoted - 1) * 100:+.1f}%) needs {100 / got:.1f}%")
    for row in every:
        shares = row["shares"] / 1e6
        lines.append(f"  {shares:.6f} x{row['n']}  needs {100 / shares:.1f}%")
    return "\n".join(lines) if len(lines) > 1 else f"*{symbol}*: no confirmed fills yet"


def clear_halt(symbol: str, ledger: str) -> str:
    conn = _open(ledger, writable=True)
    try:
        if "halt" not in _flags(conn):
            return f"{symbol}: no halt to clear"
        with conn:
            conn.execute("DELETE FROM live_flags WHERE name = 'halt'")
    finally:
        conn.close()
    return f"✅ {symbol}: halt cleared. The runner resumes on its next pass."


def reset_loss(symbol: str, ledger: str) -> str:
    """The same rule the runner's own reset applies: not while anything is unfinished."""
    conn = _open(ledger, writable=True)
    try:
        if conn.execute("SELECT 1 FROM live_flags WHERE name='continuous' AND value='1'"
                        ).fetchone() is None:
            return f"{symbol}: no continuous session to reset"
        active = _active(conn)
        if active:
            held = ", ".join(f"{p['id'][:12]} {p['state']}" for p in active)
            return (f"{symbol}: refused — {len(active)} position(s) still open ({held})."
                    " Money is still committed; finish or reconcile them first.")
        now = int(time.time())
        baseline = json.dumps([r["id"] for r in conn.execute("SELECT id FROM positions")])
        with conn:
            conn.execute("UPDATE overnight_session SET started_at=?, ends_at=?, baseline_ids=?"
                         " WHERE id=1", (now, now, baseline))
    finally:
        conn.close()
    return f"✅ {symbol}: risk session reset. Loss counter back to 0; history kept."


def tail(path: str, lines: int = 15) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, lines * 400)
            handle.seek(size - block)
            text = handle.read().decode("utf-8", "replace")
    except OSError as exc:
        return f"cannot read the log ({type(exc).__name__})"
    return "\n".join(text.splitlines()[-lines:]) or "log is empty"


class Telegram:
    """The thinnest transport that works, kept injectable so the commands can be tested."""

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def _call(self, method: str, **params):
        data = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(request, timeout=POLL_TIMEOUT + 15) as response:
            return json.load(response)

    def send(self, text: str):
        # Telegram rejects anything over 4096 characters; a truncated answer beats none.
        self._call("sendMessage", chat_id=self.chat_id, text=text[:4000],
                   parse_mode="Markdown", disable_web_page_preview="true")

    def updates(self, offset: int):
        return self._call("getUpdates", offset=offset, timeout=POLL_TIMEOUT).get("result", [])


class Control:
    """Commands over the live ledgers. Destructive ones ask twice and show their reasons."""

    def __init__(self, markets, recordings: str, logs=None):
        self.markets, self.recordings, self.logs = markets, recordings, logs or {}

    def _symbol(self, argument):
        if not argument:
            return None, ("name a market: " + ", ".join(sorted(self.markets)))
        symbol = argument.upper()
        if symbol not in self.markets:
            return None, f"unknown market {symbol}"
        return symbol, None

    def handle(self, text: str) -> str:
        parts = (text or "").split()
        if not parts:
            return self.help()
        command = parts[0].lstrip("/").split("@")[0].lower()
        argument = parts[1] if len(parts) > 1 else None
        confirmed = len(parts) > 2 and parts[2].lower() == "yes"

        if command in ("start", "help"):
            return self.help()
        if command == "status":
            return "\n\n".join(status(s, self.markets[s], self.recordings)
                               for s in sorted(self.markets))
        # Resolve the command before the market, or an unknown command answers a question
        # nobody asked — "name a market" instead of what the commands are.
        if command not in ("fills", "log", "clearhalt", "resetloss"):
            return self.help()
        symbol, problem = self._symbol(argument)
        if problem:
            return problem
        if command == "fills":
            return fills(symbol, self.markets[symbol])
        if command == "log":
            path = self.logs.get(symbol)
            return f"```\n{tail(path)}\n```" if path else f"no log configured for {symbol}"
        if command == "clearhalt":
            if not confirmed:
                return (f"{fills(symbol, self.markets[symbol])}\n\n"
                        f"{status(symbol, self.markets[symbol], self.recordings)}\n\n"
                        f"This is why trading stopped. To go on anyway:\n"
                        f"`/clearhalt {symbol} yes`")
            return clear_halt(symbol, self.markets[symbol])
        if command == "resetloss":
            if not confirmed:
                return (f"{status(symbol, self.markets[symbol], self.recordings)}\n\n"
                        f"Resetting returns the loss counter to 0 and lets it lose that"
                        f" much again. To do it:\n`/resetloss {symbol} yes`")
            return reset_loss(symbol, self.markets[symbol])
        return self.help()

    def help(self) -> str:
        return ("*commands*\n"
                "`/status` — every market at a glance\n"
                "`/fills BTC` — what each Fill actually bought\n"
                "`/log BTC` — the last lines of the runner's log\n"
                "`/clearhalt BTC` — show why it stopped, then `yes` to resume\n"
                "`/resetloss BTC` — reset the risk session, only with nothing open")


def alerts(control: Control, before: dict) -> tuple:
    """What changed that a person would want woken for, and the state to compare next."""
    now, messages = {}, []
    for symbol, ledger in sorted(control.markets.items()):
        conn = _open(ledger)
        try:
            halt = _flags(conn).get("halt")
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        age = feed_age(control.recordings, symbol)
        now[symbol] = {"halt": halt, "stale": age is not None and age > FEED_SILENCE_SECONDS}
        was = before.get(symbol, {})
        if halt and was.get("halt") != halt:
            messages.append(f"⛔ *{symbol} stopped trading*\n{halt}\n\n"
                            f"`/clearhalt {symbol}` to see why and resume")
        if now[symbol]["stale"] and not was.get("stale"):
            messages.append(f"⚠️ *{symbol} feed is stale* ({age}s). Nothing will trade"
                            f" until it returns; this is usually the venue, not us.")
        if was.get("stale") and not now[symbol]["stale"]:
            messages.append(f"✅ *{symbol} feed is back*")
    return messages, now


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--market", action="append", default=[], metavar="SYMBOL=LEDGER[:LOG]",
                        help="repeat per market, e.g. BTC=data/live-BTC.db:data/BTC-continuous.log")
    parser.add_argument("--alert-seconds", type=float, default=60)
    args = parser.parse_args()

    token = os.environ.get("NINELIVES_TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("NINELIVES_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("set NINELIVES_TELEGRAM_TOKEN and NINELIVES_TELEGRAM_CHAT_ID")

    markets, logs = {}, {}
    for entry in args.market:
        symbol, _, rest = entry.partition("=")
        ledger, _, log_path = rest.partition(":")
        if not symbol or not ledger:
            raise SystemExit(f"bad --market {entry!r}; expected SYMBOL=LEDGER[:LOG]")
        markets[symbol.upper()] = ledger
        if log_path:
            logs[symbol.upper()] = log_path
    if not markets:
        raise SystemExit("give at least one --market")

    telegram, control = Telegram(token, chat_id), Control(markets, args.recordings, logs)
    offset, seen, checked = 0, {}, 0.0
    telegram.send("🟢 monitor up\n" + control.help())
    while True:
        try:
            for update in telegram.updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                # One chat may command this. Anything else is read and dropped in silence,
                # which tells a stranger nothing about whether they found anything.
                if str((message.get("chat") or {}).get("id")) != chat_id:
                    continue
                telegram.send(control.handle(message.get("text", "")))
            if time.time() - checked >= args.alert_seconds:
                checked = time.time()
                messages, seen = alerts(control, seen)
                for text in messages:
                    telegram.send(text)
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            # A monitor that dies on a dropped connection is worse than no monitor.
            time.sleep(5)


if __name__ == "__main__":
    main()
