"""
APEX Sweep Scanner
-------------------
Detects the "previous candle sweep" pattern (D1 -> H4) across a watchlist
and sends a formatted Telegram alert, styled after the slkradar-bot format.

Rule:
  - Take the last CLOSED daily (D1) candle's high/low.
  - Watch the most recently CLOSED 4-hour (H4) candle.
  - SELL bias  : H4 wicks above the D1 high, then H4 CLOSES back below it.
  - BUY bias   : H4 wicks below the D1 low,  then H4 CLOSES back above it.
  - This is a bias/context flag only — NOT an entry signal.

Data source: Twelve Data (free tier: 800 calls/day, 8 calls/min).
Delivery: Telegram Bot API (sendMessage).
State: data/state.json — prevents re-alerting the same D1/H4 combo on every run.
        Must be committed back to the repo by the GitHub Actions workflow.
"""

import os
import json
import time
import sys
from datetime import datetime, timezone, timedelta
import requests

WAT = timezone(timedelta(hours=1))  # West Africa Time, fixed UTC+1, no DST

# ============================================================
# WATCHLIST — edit this to match what you actually want scanned.
#
# "twelvedata_symbol" MUST be verified against Twelve Data's own
# symbol search before you trust it — especially for indices, where
# naming varies a lot by provider (e.g. "UK100" vs "FTSE" vs "GB100").
# Check: https://api.twelvedata.com/symbol_search?symbol=FTSE
# Forex pairs are reliable as "EUR/CAD" style, so those below are safe.
# The index rows are placeholders — confirm before relying on them.
# ============================================================
WATCHLIST = [
    {"display": "EURCAD", "twelvedata_symbol": "EUR/CAD"},
    {"display": "CADCHF", "twelvedata_symbol": "CAD/CHF"},
    {"display": "GBPUSD", "twelvedata_symbol": "GBP/USD"},
    {"display": "USDJPY", "twelvedata_symbol": "USD/JPY"},
    # --- verify these two before trusting alerts on them ---
    {"display": "UK100", "twelvedata_symbol": "UK100"},
    {"display": "JPN225", "twelvedata_symbol": "JPN225"},
]

TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "state.json")
BASE_URL = "https://api.twelvedata.com/time_series"
TZ_NAME = "Africa/Lagos"  # WAT, UTC+1, no DST — Twelve Data returns datetimes already in this tz


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def fetch_series(symbol, interval, outputsize=3):
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": TZ_NAME,
        "apikey": TWELVEDATA_API_KEY,
        "order": "desc",  # most recent first
    }
    resp = requests.get(BASE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error for {symbol} ({interval}): {data.get('message')}")
    values = data.get("values")
    if not values or len(values) < 2:
        raise RuntimeError(f"Not enough {interval} data returned for {symbol}")
    return values  # values[0] = most recent (may still be forming), values[1] = last closed


def fmt_price(x):
    return f"{float(x):.5f}" if abs(float(x)) < 100 else f"{float(x):.2f}"


def build_message(display, direction, rejection_dt, external_bo_dt, level, swept_price, run_time_str):
    arrow = "🔻" if direction == "SELL" else "🔺"
    swept_label = "high" if direction == "SELL" else "low"
    close_action = "closed back below" if direction == "SELL" else "closed back above"
    return (
        f"BIAS CONFIRMED - EXTERNAL BO {arrow} {direction} {display} (D1->H4)\n\n"
        f"Rule       : Previous candle sweep\n"
        f"Rejection  : {rejection_dt} WAT\n"
        f"External BO: {external_bo_dt} WAT\n"
        f"Level      : {fmt_price(level)}\n\n"
        f"swept previous candle {swept_label} {fmt_price(swept_price)}, {close_action}\n\n"
        f"Not an entry signal, look for your entry model.\n"
        f"\u23f0 Alert generated: {run_time_str} WAT"
    )


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    resp.raise_for_status()


def check_symbol(entry, state, run_time_str):
    display = entry["display"]
    symbol = entry["twelvedata_symbol"]

    try:
        d1 = fetch_series(symbol, "1day", outputsize=3)
        time.sleep(8)  # stay well under Twelve Data's 8 calls/minute free-tier limit
        h4 = fetch_series(symbol, "4h", outputsize=3)
        time.sleep(8)
    except Exception as e:
        print(f"[{display}] skipped: {e}")
        return state

    prev_d1 = d1[1]  # last fully closed daily candle
    d1_high, d1_low, d1_time = float(prev_d1["high"]), float(prev_d1["low"]), prev_d1["datetime"]

    last_h4 = h4[1]  # last fully closed H4 candle (h4[0] may still be forming)
    h4_high, h4_low, h4_close, h4_time = (
        float(last_h4["high"]), float(last_h4["low"]), float(last_h4["close"]), last_h4["datetime"]
    )

    sell = h4_high > d1_high and h4_close < d1_high
    buy = h4_low < d1_low and h4_close > d1_low

    if not (sell or buy):
        return state

    direction = "SELL" if sell else "BUY"
    signature = f"{d1_time}|{h4_time}|{direction}"

    if state.get(display) == signature:
        print(f"[{display}] {direction} bias already alerted for this candle pair — skipping")
        return state

    level = h4_close
    swept_price = d1_high if sell else d1_low

    msg = build_message(display, direction, d1_time, h4_time, level, swept_price, run_time_str)
    send_telegram(msg)
    print(f"[{display}] sent {direction} alert")

    state[display] = signature
    return state


def main():
    state = load_state()
    run_time_str = datetime.now(timezone.utc).astimezone(WAT).strftime("%d %b %Y, %I:%M %p")

    for entry in WATCHLIST:
        state = check_symbol(entry, state, run_time_str)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
