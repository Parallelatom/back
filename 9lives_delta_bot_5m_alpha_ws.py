import requests
import time
import csv
import os
import math
import asyncio
import json
import threading
from datetime import datetime
from collections import deque

try:
    import websockets
except ImportError:
    websockets = None

# ---------------- CONFIG ----------------

GRAPH_URL = "https://graph.9lives.so/"
TRADE_URL = "https://accounts.superposition.so/"
WS_URL = "wss://websocket.9lives.so/"

SYMBOLS = ["BTC"]
CATEGORY = "5mins"

API_KEY = ""

TRADE_SIZE_USDC = 1
ORACLE_DELAY = 5
SNAPSHOT_FILE = "market_snapshot_live_ws_5m.csv"
RISK_STATE_FILE = "risk_state_5m.json"

# 🔥 WINDOW (LOCK ≥60s)
TRADE_WINDOW_START = 100
TRADE_WINDOW_END = 60

EDGE_THRESHOLD = 0.05
EARLY_EDGE_THRESHOLD = 0.15
DELTA_HOLD_SECONDS = 3
SCAN_INTERVAL_SECONDS = 1
WS_PRICE_MAX_AGE_SECONDS = 10
WS_PING_INTERVAL_SECONDS = 15
WS_PING_TIMEOUT_SECONDS = 10
WS_RETRY_MIN_SECONDS = 1
WS_RETRY_MAX_SECONDS = 30

MAX_LOSS_STREAK = 2
PAUSE_TIME = 3600
MAX_ACTIVE_TRADES = 1

DELTA_FILTER_PERCENT = {
    "BTC": 0.08
}

WS_HEADERS = {
    "Origin": "https://9lives.so",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

WS_SUBSCRIPTIONS = [
    {
        "label": symbol,
        "ask_for_snapshot": [
            {
                "table": "oracles_ninelives_prices_2",
                "fields": [{"name": "base", "filter_constraints": {"et": symbol}}]
            }
        ],
        "add": [
            {
                "table": "oracles_ninelives_prices_2",
                "fields": [{"name": "base", "filter_constraints": {"et": symbol}}]
            }
        ]
    }
    for symbol in SYMBOLS
]

# ---------------- STATE ----------------

session = requests.Session()
ws_prices = {}
ws_event_counts = {}
ws_price_lock = threading.Lock()
ws_started = False

trades = {}
executed_rounds = set()

losing_streak = 0
pause_until = 0

total_trades = 0
wins = 0
losses = 0

# ---------------- PRICE CACHE ----------------

price_cache = {}
MAX_CACHE_SECONDS = 60
delta_hold_state = {}

def delta_filter_held(symbol, ending, delta_pct):

    now = time.time()
    threshold = DELTA_FILTER_PERCENT.get(symbol, 0.03)
    direction = 1 if delta_pct > 0 else -1
    state = delta_hold_state.get(symbol)

    if abs(delta_pct) <= threshold:
        delta_hold_state.pop(symbol, None)
        return False

    if (
        not state
        or state["ending"] != ending
        or state["direction"] != direction
    ):
        delta_hold_state[symbol] = {
            "ending": ending,
            "direction": direction,
            "since": now
        }
        return False

    return now - state["since"] >= DELTA_HOLD_SECONDS

def update_price_cache(symbol, price, strike):
    now = int(time.time())

    if symbol not in price_cache:
        price_cache[symbol] = deque()

    dq = price_cache[symbol]
    dq.append((now, price, strike))

    while dq and now - dq[0][0] > MAX_CACHE_SECONDS:
        dq.popleft()

def calc_momentum(symbol, window):

    if symbol not in price_cache:
        return 0

    dq = price_cache[symbol]
    if len(dq) < 2:
        return 0

    now = dq[-1][0]

    old = None
    for t,p,s in dq:
        if now - t >= window:
            old = (t,p,s)
            break

    if not old:
        return 0

    _, p_old, strike = old
    _, p_now, _ = dq[-1]

    return (p_now - p_old) / strike

def get_momentum(symbol):
    return (
        calc_momentum(symbol,10),
        calc_momentum(symbol,30),
        calc_momentum(symbol,60)
    )

# ---------------- FILE ----------------

def load_risk_state():
    global losing_streak, pause_until

    if not os.path.isfile(RISK_STATE_FILE):
        return

    try:
        with open(RISK_STATE_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return

    losing_streak = int(data.get("losing_streak", 0) or 0)
    pause_until = int(data.get("pause_until", 0) or 0)

def save_risk_state():
    try:
        with open(RISK_STATE_FILE, "w") as f:
            json.dump({
                "losing_streak": losing_streak,
                "pause_until": pause_until,
                "updated_at": int(time.time())
            }, f)
    except Exception as e:
        print("Risk state save error:", e)

def save_trade(symbol, price, strike, delta_pct, side, result):

    file = "trade_history_live_5m.csv"
    exists = os.path.isfile(file)

    with open(file, "a", newline="") as f:
        writer = csv.writer(f)

        if not exists:
            writer.writerow(["time","symbol","price","strike","delta_pct","side","result"])

        writer.writerow([
            int(time.time()),
            symbol,
            price,
            strike,
            round(delta_pct, 6),
            side,
            result
        ])

def save_market_snapshot(
    symbol,
    ending,
    time_left,
    price,
    strike,
    delta_pct,
    threshold_pct,
    market_prob,
    price_source,
    m10,
    m30,
    m60,
    delta_hold_ok,
    in_trade_window,
    signal,
    side,
    reason
):

    exists = os.path.isfile(SNAPSHOT_FILE)

    with open(SNAPSHOT_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        if not exists:
            writer.writerow([
                "timestamp",
                "symbol",
                "ending",
                "time_left",
                "price",
                "strike",
                "delta_pct",
                "threshold_pct",
                "market_prob",
                "price_source",
                "m10",
                "m30",
                "m60",
                "delta_hold_ok",
                "in_trade_window",
                "signal",
                "side",
                "reason"
            ])

        writer.writerow([
            int(time.time()),
            symbol,
            ending,
            time_left,
            price,
            strike,
            round(delta_pct, 6),
            threshold_pct,
            round(market_prob, 6) if market_prob is not None else "",
            price_source,
            round(m10, 8),
            round(m30, 8),
            round(m60, 8),
            int(delta_hold_ok),
            int(in_trade_window),
            signal,
            side,
            reason
        ])

# ---------------- NETWORK ----------------

def safe_post(url,payload,headers=None):
    for _ in range(3):
        try:
            r = session.post(url,json=payload,headers=headers,timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print("HTTP error:",e)
        time.sleep(1)
    return None

# ---------------- DATA / DELTA ----------------

def build_market_query():

    market_queries = []
    for index, symbol in enumerate(SYMBOLS):
        market_queries.append(
            f"""
          m{index}: campaignBySymbol(symbol:"{symbol}",category:"{CATEGORY}"){{
            poolAddress odds ending winner totalVolume
            priceMetadata{{ priceTargetForUp }}
            outcomes{{ identifier name }}
          }}
            """
        )

    return {
        "query": f"""
        query {{
          assetsDeltaHour {{ name price }}
          {"".join(market_queries)}
        }}
        """
    }

def find_price(assets, symbol):

    symbol_upper = symbol.upper()

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        name = str(asset.get("name", "")).upper()
        asset_price = asset.get("price")

        if symbol_upper in name and asset_price is not None:
            try:
                return float(asset_price)
            except (TypeError, ValueError):
                continue

    return None

def calc_delta_pct(price, strike):

    if strike is None or strike == 0:
        return None

    return (price - strike) / strike * 100

# ---------------- WEBSOCKET PRICE ----------------

def _ws_version():
    if websockets is None:
        return (0, 0)

    try:
        return tuple(int(x) for x in websockets.__version__.split(".")[:2])
    except Exception:
        return (0, 0)

async def _connect_ws():
    kwargs = {
        "ping_interval": WS_PING_INTERVAL_SECONDS,
        "ping_timeout": WS_PING_TIMEOUT_SECONDS,
    }

    try:
        return await websockets.connect(WS_URL, additional_headers=WS_HEADERS, **kwargs)
    except TypeError:
        return await websockets.connect(WS_URL, extra_headers=WS_HEADERS, **kwargs)

def _set_ws_price(symbol, price):
    try:
        price = float(price)
    except (TypeError, ValueError):
        return

    with ws_price_lock:
        ws_prices[symbol] = {
            "price": price,
            "updated_at": time.time()
        }
        ws_event_counts[symbol] = ws_event_counts.get(symbol, 0) + 1

def get_ws_price(symbol):
    with ws_price_lock:
        item = ws_prices.get(symbol)

    if not item:
        return None

    if time.time() - item["updated_at"] > WS_PRICE_MAX_AGE_SECONDS:
        return None

    return item["price"]

def has_fresh_ws_price(symbol):
    return get_ws_price(symbol) is not None

def get_ws_prices():
    return {
        symbol: price
        for symbol in SYMBOLS
        for price in [get_ws_price(symbol)]
        if price is not None
    }

async def listen_ws_prices(sub):
    label = sub["label"]
    retry = WS_RETRY_MIN_SECONDS

    while True:
        try:
            ws = await _connect_ws()
            async with ws:
                await ws.send(json.dumps({
                    "ask_for_snapshot": sub["ask_for_snapshot"],
                    "add": sub["add"]
                }))

                retry = WS_RETRY_MIN_SECONDS

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if data.get("table") != "oracles_ninelives_prices_2":
                        continue

                    content = data.get("content") or {}
                    rows = content if isinstance(content, list) else [content]

                    for row in rows:
                        if not isinstance(row, dict):
                            continue

                        base = str(row.get("base", "")).upper()
                        amount = row.get("amount")

                        if base == label and amount is not None:
                            _set_ws_price(label, amount)

        except Exception as e:
            print(f"WS {label} error:", e)

        await asyncio.sleep(retry)
        retry = min(retry * 2, WS_RETRY_MAX_SECONDS)

async def ws_price_main():
    if websockets is None:
        print("WS disabled: install websockets to use realtime prices")
        return

    await asyncio.gather(*[
        listen_ws_prices(sub)
        for sub in WS_SUBSCRIPTIONS
    ])

def start_ws_price_thread():
    global ws_started

    if ws_started:
        return

    ws_started = True
    thread = threading.Thread(
        target=lambda: asyncio.run(ws_price_main()),
        daemon=True
    )
    thread.start()

def get_market_data():

    data = safe_post(GRAPH_URL,build_market_query())
    if not data:
        return None,None

    data = data.get("data") or {}

    graph_prices = {}
    assets = data.get("assetsDeltaHour") or []
    for symbol in SYMBOLS:
        price = find_price(assets, symbol)
        if price is not None:
            graph_prices[symbol] = price

    prices = graph_prices.copy()
    prices.update(get_ws_prices())

    markets = {}
    for index, symbol in enumerate(SYMBOLS):
        market = data.get(f"m{index}")
        markets[symbol] = market if isinstance(market, dict) else None

    return prices,markets

# ---------------- PROB ----------------

def extract_market_prob(odds):

    if odds is None:
        return None

    try:
        vals = odds.values() if isinstance(odds, dict) else odds
        vals = [float(v) for v in vals if v is not None]
    except:
        return None

    if len(vals) < 2:
        return None

    total = sum(vals)
    if total == 0:
        return None

    return vals[0] / total

# ---------------- MODEL ----------------

def hybrid_model(symbol, price, strike, time_left, market_prob):

    delta_pct = calc_delta_pct(price, strike)
    if delta_pct is None:
        return None

    dist = delta_pct / 100
    m10, m30, m60 = get_momentum(symbol)

    # 🔒 LOCK <60s
    if time_left < TRADE_WINDOW_END:
        return None

    if abs(delta_pct) < DELTA_FILTER_PERCENT.get(symbol,0.03):
        return None

    if abs(m10) < 0.0002:
        return None

    # trend confirm
    if not ((m10>0 and m30>0) or (m10<0 and m30<0)):
        return None

    if dist > 0 and m10 < 0:
        return None
    if dist < 0 and m10 > 0:
        return None

    strength = abs(m10)*0.5 + abs(m30)*0.3 + abs(m60)*0.2
    if strength < 0.0006:
        return None

    k = 900
    prob = 1/(1+math.exp(-k*dist))

    edge = prob - market_prob
    if abs(edge) < EDGE_THRESHOLD:
        return None

    payout = (1/market_prob)-1
    ev = (prob*payout) - ((1-prob)*1)

    if ev <= 0:
        return None

    return prob

# ---------------- EXECUTE ----------------

def execute_trade(symbol,market,price,strike,delta_pct,prob,ending):

    global total_trades

    if time.time() < pause_until:
        print("ORDER SKIP: paused")
        return False

    if len(trades) >= MAX_ACTIVE_TRADES:
        print("ORDER SKIP: active trade limit")
        return False

    trade_id = f"{symbol}_{ending}"
    if trade_id in executed_rounds:
        return False

    side = "UP" if prob > 0.5 else "DOWN"

    outcomes = {}
    for o in market.get("outcomes",[]):
        name=o.get("name","").lower()
        if "above" in name:
            outcomes["UP"]=o.get("identifier")
        if "below" in name:
            outcomes["DOWN"]=o.get("identifier")

    outcome = outcomes.get(side)
    if not outcome:
        return False

    payload={
        "query":"mutation ($mint: Mint!) { ninelivesMint(mint: $mint) }",
        "variables":{
            "mint":{
                "amount":str(int(TRADE_SIZE_USDC*1_000_000)),
                "market":market.get("poolAddress"),
                "referrer":"0x0000000000000000000000000000000000000000",
                "ms_ts":str(int(time.time()*1000)),
                "outcome":outcome
            }
        }
    }

    headers={
        "content-type":"application/json",
        "authorization":API_KEY
    }

    result = safe_post(TRADE_URL,payload,headers)
    if not result:
        print("ORDER FAIL")
        return False

    print(f"🔥 TRADE {symbol} {side}")

    trades[trade_id] = {
        "symbol":symbol,
        "price":price,
        "strike":strike,
        "delta_pct":delta_pct,
        "side":side,
        "ending":ending
    }

    executed_rounds.add(trade_id)
    total_trades += 1
    return True

# ---------------- SETTLEMENT ----------------

def get_final_price(symbol,ending):

    payload={
        "query":"query ($symbol: String!, $ending: Int!) { getFinalPrice(symbol: $symbol, ending: $ending) }",
        "variables":{
            "symbol":symbol.lower(),
            "ending":ending*1000
        }
    }

    data=safe_post(GRAPH_URL,payload)
    if not data:
        return None

    graph_data = data.get("data") or {}
    final_price = graph_data.get("getFinalPrice")
    if final_price is None:
        return None

    try:
        return float(final_price)
    except (TypeError, ValueError):
        return None

def check_settlement():

    global losing_streak, pause_until, wins, losses

    now=int(time.time())

    for tid,t in list(trades.items()):

        if now < t["ending"] + ORACLE_DELAY:
            continue

        final = get_final_price(t["symbol"],t["ending"])
        if final is None:
            continue

        win = "UP" if final > t["strike"] else "DOWN"
        result = "WIN" if win == t["side"] else "LOSE"

        print("🟢 SETTLED",tid,result)

        save_trade(t["symbol"], t["price"], t["strike"], t.get("delta_pct", 0), t["side"], result)

        if result == "WIN":
            wins += 1
            losing_streak = 0
        else:
            losses += 1
            losing_streak += 1

        if losing_streak >= MAX_LOSS_STREAK:
            pause_until = int(time.time()) + PAUSE_TIME
            print("⛔ PAUSED")
            losing_streak = 0

        save_risk_state()

        del trades[tid]

# ---------------- SCAN ----------------

def scan():

    global pause_until

    if time.time() < pause_until:
        print("⏸ PAUSED")
        return 0

    if len(trades) >= MAX_ACTIVE_TRADES:
        print("ACTIVE TRADE LIMIT: waiting for settlement")
        return 0, 0

    prices, markets = get_market_data()

    if not prices or not markets:
        return 0, 0

    markets_seen = 0
    trades_triggered = 0

    for symbol, market in markets.items():

        try:
            if not market or market.get("winner") is not None:
                continue

            if symbol not in prices:
                continue

            markets_seen += 1

            price = prices[symbol]
            price_source = "WS" if has_fresh_ws_price(symbol) else "GRAPH_FALLBACK"

            price_metadata = market.get("priceMetadata") or {}
            strike_raw = price_metadata.get("priceTargetForUp")
            ending_raw = market.get("ending")
            if strike_raw is None or ending_raw is None:
                continue

            try:
                strike = float(strike_raw)
                ending = int(ending_raw)
            except (TypeError, ValueError):
                continue

            delta_pct = calc_delta_pct(price, strike)
            if delta_pct is None:
                continue

            time_left = ending - int(time.time())
            threshold_pct = DELTA_FILTER_PERCENT.get(symbol, 0.03)

            if time_left <= 0:
                continue

            update_price_cache(symbol, price, strike)
            m10, m30, m60 = get_momentum(symbol)
            market_prob = extract_market_prob(market.get("odds"))
            in_trade_window = (
                time_left <= TRADE_WINDOW_START
                and time_left >= TRADE_WINDOW_END
            )

            # 🔒 WINDOW
            if not in_trade_window:
                save_market_snapshot(
                    symbol, ending, time_left, price, strike, delta_pct,
                    threshold_pct, market_prob, price_source, m10, m30, m60,
                    False, False, "NO_TRADE", "", "outside_trade_window"
                )
                continue

            delta_hold_ok = delta_filter_held(symbol, ending, delta_pct)
            if not delta_hold_ok:
                save_market_snapshot(
                    symbol, ending, time_left, price, strike, delta_pct,
                    threshold_pct, market_prob, price_source, m10, m30, m60,
                    False, True, "NO_TRADE", "", "delta_hold_wait"
                )
                continue

            trade_id = f"{symbol}_{ending}"
            if trade_id in executed_rounds:
                save_market_snapshot(
                    symbol, ending, time_left, price, strike, delta_pct,
                    threshold_pct, market_prob, price_source, m10, m30, m60,
                    True, True, "NO_TRADE", "", "already_executed_round"
                )
                continue

            # 🟡 EARLY
            if market_prob is None:

                dist = delta_pct / 100
                if abs(delta_pct) < DELTA_FILTER_PERCENT.get(symbol,0.03)*1.5:
                    save_market_snapshot(
                        symbol, ending, time_left, price, strike, delta_pct,
                        threshold_pct, market_prob, price_source, m10, m30, m60,
                        True, True, "NO_TRADE", "", "early_delta_too_small"
                    )
                    continue

                if not ((m10>0 and m30>0) or (m10<0 and m30<0)):
                    save_market_snapshot(
                        symbol, ending, time_left, price, strike, delta_pct,
                        threshold_pct, market_prob, price_source, m10, m30, m60,
                        True, True, "NO_TRADE", "", "early_trend_not_confirmed"
                    )
                    continue

                if abs(m10) < 0.0003:
                    save_market_snapshot(
                        symbol, ending, time_left, price, strike, delta_pct,
                        threshold_pct, market_prob, price_source, m10, m30, m60,
                        True, True, "NO_TRADE", "", "early_m10_too_weak"
                    )
                    continue

                prob = 0.85 if dist > 0 else 0.15
                edge = abs(prob - 0.5)

                if edge < EARLY_EDGE_THRESHOLD:
                    save_market_snapshot(
                        symbol, ending, time_left, price, strike, delta_pct,
                        threshold_pct, market_prob, price_source, m10, m30, m60,
                        True, True, "NO_TRADE", "", "early_edge_too_small"
                    )
                    continue

                print(f"🟡 EARLY {symbol}")

                side = "UP" if prob > 0.5 else "DOWN"
                save_market_snapshot(
                    symbol, ending, time_left, price, strike, delta_pct,
                    threshold_pct, market_prob, price_source, m10, m30, m60,
                    True, True, "TRADE", side, "early_signal"
                )
                if execute_trade(symbol, market, price, strike, delta_pct, prob, ending):
                    trades_triggered += 1
                continue

            # 🟢 LATE
            prob = hybrid_model(symbol, price, strike, time_left, market_prob)

            if prob is None:
                save_market_snapshot(
                    symbol, ending, time_left, price, strike, delta_pct,
                    threshold_pct, market_prob, price_source, m10, m30, m60,
                    True, True, "NO_TRADE", "", "hybrid_rejected"
                )
                continue

            side = "UP" if prob > 0.5 else "DOWN"
            save_market_snapshot(
                symbol, ending, time_left, price, strike, delta_pct,
                threshold_pct, market_prob, price_source, m10, m30, m60,
                True, True, "TRADE", side, "hybrid_signal"
            )
            if execute_trade(symbol, market, price, strike, delta_pct, prob, ending):
                trades_triggered += 1

        except Exception as e:
            print(f"❌ ERROR {symbol}: {e}")

    return markets_seen, trades_triggered

# ---------------- LOOP ----------------

load_risk_state()
start_ws_price_thread()

while True:

    start = time.time()

    print("\n🔄 SCAN", time.strftime("%H:%M:%S"))

    check_settlement()

    scan_result = scan()
    if isinstance(scan_result, tuple):
        markets_seen, trades_triggered = scan_result
    else:
        markets_seen, trades_triggered = scan_result, 0

    total = wins + losses
    winrate = (wins/total*100) if total > 0 else 0

    print(
        "\n📊 STATUS",
        "\n markets_seen=", markets_seen,
        "\n trades_triggered=", trades_triggered,
        "\n active_trades=", len(trades),
        "\n total_trades=", total_trades,
        "\n win=", wins,
        "\n lose=", losses,
        "\n winrate=", round(winrate,2),
        "\n scan_time=", round(time.time()-start,3), "s"
    )

    time.sleep(SCAN_INTERVAL_SECONDS)
