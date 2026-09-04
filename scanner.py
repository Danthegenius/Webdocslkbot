"""
APEX Structure Scanner (v2)
----------------------------
Implements: Daily storyline (rejection from a daily keylevel) confirmed by
a 4H BOS (body close beyond the swing that started the reactive leg).

This is the "mid-week" pipeline per your own rule: start from the daily
storyline, confirm on 4H. The Weekly -> Daily confirmation layer (used at
the start of a new week) is NOT included yet — flagged as a deliberate
first-pass scope cut, not an oversight.

Pipeline per symbol, per run:
  - No pending setup?  -> scan for a fresh daily rejection off the current
    daily keylevel. If found, compute the origin swing (where the leg into
    the keylevel started) and store it as "pending", waiting on a 4H BOS.
  - Pending setup exists? -> look at 4H bars since the rejection:
      1. First confirm the 4H origin swing (the small pullback high/low
         that forms shortly after the daily rejection).
      2. Once that exists, watch for a 4H candle body-closing beyond it —
         that's the BOS. Fire the alert, clear the pending setup.
  - A pending setup with no BOS after EXPIRY_DAYS is dropped (stale).

State persisted in data/state.json so pending setups survive across the
30-min scheduled runs.
"""

import os
import json
import time
import sys
from datetime import datetime, timezone, timedelta

import requests

from structure import find_swings, active_keylevels, origin_swing, check_rejection, check_bos

WAT = timezone(timedelta(hours=1))

# ============================================================
# WATCHLIST - same symbols as before. Edit freely.
# ============================================================
WATCHLIST = [
    {"display": "GBPUSD", "twelvedata_symbol": "GBP/USD"},
    {"display": "USDJPY", "twelvedata_symbol": "USD/JPY"},
    {"display": "EURUSD", "twelvedata_symbol": "EUR/USD"},
    {"display": "AUDUSD", "twelvedata_symbol": "AUD/USD"},
    {"display": "XAUUSD", "twelvedata_symbol": "XAU/USD"},
    {"display": "BTCUSD", "twelvedata_symbol": "BTC/USD"},
]

DAILY_LOOKBACK = 3     # candles each side to confirm a daily swing
EXPIRY_DAYS = 5         # drop a pending setup if no BOS within this many calendar days

TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "state.json")
BASE_URL = "https://api.twelvedata.com/time_series"
TZ_NAME = "Africa/Lagos"


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_series_asc(symbol, interval, outputsize):
    """Returns bars OLDEST -> NEWEST, the order structure.py expects."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": TZ_NAME,
        "apikey": TWELVEDATA_API_KEY,
        "order": "asc",
    }
    resp = requests.get(BASE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error for {symbol} ({interval}): {data.get('message')}")
    values = data.get("values")
    if not values or len(values) < (2 * DAILY_LOOKBACK + 5):
        raise RuntimeError(f"Not enough {interval} data returned for {symbol}")
    bars = [
        {"datetime": v["datetime"], "high": float(v["high"]), "low": float(v["low"]), "close": float(v["close"])}
        for v in values
    ]
    return bars


def fmt_price(x):
    return f"{x:.5f}" if abs(x) < 100 else f"{x:.2f}"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    resp.raise_for_status()


def build_message(display, direction, keylevel_price, keylevel_time, origin_price, origin_time,
                   rejection_time, bos_time, run_time_str):
    arrow = "\U0001F53B" if direction == "SELL" else "\U0001F53A"
    return (
        f"BIAS CONFIRMED - 4H BOS {arrow} {direction} {display} (D1->H4)\n\n"
        f"Rule        : Daily rejection + 4H break of structure\n"
        f"Daily keylevel : {fmt_price(keylevel_price)} ({keylevel_time} WAT)\n"
        f"Rejection      : {rejection_time} WAT\n"
        f"Origin swing   : {fmt_price(origin_price)} ({origin_time} WAT)\n"
        f"4H BOS         : {bos_time} WAT\n\n"
        f"Daily rejected off keylevel, 4H closed back through the origin "
        f"of the move that led into it.\n\n"
        f"Not an entry signal, look for your entry model.\n"
        f"\u23f0 Alert generated: {run_time_str} WAT"
    )


def is_expired(pending, now):
    since = datetime.fromisoformat(pending["rejection_time"].replace(" ", "T"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=WAT)
    return (now - since) > timedelta(days=EXPIRY_DAYS)


def process_symbol(entry, state, run_time_str, now):
    display = entry["display"]
    symbol = entry["twelvedata_symbol"]

    try:
        d1 = fetch_series_asc(symbol, "1day", outputsize=120)
        time.sleep(8)
        h4 = fetch_series_asc(symbol, "4h", outputsize=150)
        time.sleep(8)
    except Exception as e:
        print(f"[{display}] skipped: {e}")
        return state

    sym_state = state.get(display, {})
    pending = sym_state.get("pending")

    if pending and is_expired(pending, now):
        print(f"[{display}] pending {pending['direction']} setup expired (no BOS within {EXPIRY_DAYS}d) - dropping")
        pending = None

    if pending is None:
        prior_bars = d1[:-1]  # structure as known BEFORE today's (still-forming or just-closed) candle
        swings = find_swings(prior_bars, lookback=DAILY_LOOKBACK)
        latest_high, latest_low = active_keylevels(prior_bars, swings)

        rejection = None
        keylevel = None
        direction = None

        if latest_high:
            r = check_rejection(d1, latest_high)
            if r:
                rejection, keylevel, direction = r, latest_high, "SELL"

        if rejection is None and latest_low:
            r = check_rejection(d1, latest_low)
            if r:
                rejection, keylevel, direction = r, latest_low, "BUY"

        if rejection:
            origin = origin_swing(swings, keylevel)
            if origin:
                pending = {
                    "direction": direction,
                    "keylevel_price": keylevel["price"],
                    "keylevel_time": keylevel["datetime"],
                    "origin_price": origin["price"],
                    "origin_time": origin["datetime"],
                    "rejection_time": rejection["datetime"],
                }
                sym_state["pending"] = pending
                print(f"[{display}] NEW {direction} daily rejection at {rejection['datetime']} "
                      f"(keylevel {fmt_price(keylevel['price'])}, origin {fmt_price(origin['price'])})")
            else:
                print(f"[{display}] rejection seen but no origin swing available yet")
        else:
            print(f"[{display}] no setup")

    else:
        direction = pending["direction"]
        rejection_time = pending["rejection_time"]
        h4_after = [b for b in h4 if b["datetime"] > rejection_time]

        # Watch 4H candles for the first body close beyond the SAME origin
        # swing already established from the daily structure - this is
        # rule 4's "confirm on 4hr", not a separate 4H-scale swing.
        bos_bar = check_bos(h4_after, {"price": pending["origin_price"]}, direction)

        if bos_bar:
            msg = build_message(
                display, direction,
                pending["keylevel_price"], pending["keylevel_time"],
                pending["origin_price"], pending["origin_time"],
                pending["rejection_time"], bos_bar["datetime"],
                run_time_str,
            )
            send_telegram(msg)
            print(f"[{display}] 4H BOS CONFIRMED at {bos_bar['datetime']} -> alert sent")
            pending = None
        else:
            print(f"[{display}] pending {direction} since {rejection_time} - waiting for 4H BOS through {fmt_price(pending['origin_price'])}")

        sym_state["pending"] = pending

    state[display] = sym_state
    return state


def main():
    state = load_state()
    now = datetime.now(timezone.utc).astimezone(WAT)
    run_time_str = now.strftime("%d %b %Y, %I:%M %p")

    for entry in WATCHLIST:
        state = process_symbol(entry, state, run_time_str, now)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
