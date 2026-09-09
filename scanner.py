"""
APEX Structure Scanner (v3)
---------------------------
Weekly -> Daily -> 4H pipeline.

Changes from v2, all deliberate:

  * 4H BOS is now a 4H-SCALE break (close through the most recent confirmed 4H
    level), not a close through the DAILY origin swing. v2 required a 4H candle
    to close 1-5% beyond the rejection, which is why setups expired unfired.
  * Rejections are evaluated on CLOSED candles. The still-forming daily candle
    is still allowed (branch 2) but the alert is marked PROVISIONAL.
  * Cross-timeframe ordering uses parsed datetimes. v2 compared '2026-09-08'
    against '2026-09-08 12:00:00' as strings, which let 4H bars from inside
    the rejection day count as confirmation.
  * A pending setup dies if price closes back through the daily keylevel.
  * Weekly and daily bars are cached to disk; only 4H is fetched every run.
  * Alerts are deduped by signature so a setup fires once, not every 30 min.
"""

import os
import json
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))


from context import build_context
from resolver import resolve

WAT = timezone(timedelta(hours=1))

WATCHLIST = [
    {"display": "EURUSD", "twelvedata_symbol": "EUR/USD"},
    {"display": "GBPUSD", "twelvedata_symbol": "GBP/USD"},
    {"display": "USDJPY", "twelvedata_symbol": "USD/JPY"},
    {"display": "USDCHF", "twelvedata_symbol": "USD/CHF"},
    {"display": "USDCAD", "twelvedata_symbol": "USD/CAD"},
    {"display": "AUDUSD", "twelvedata_symbol": "AUD/USD"},
    {"display": "NZDUSD", "twelvedata_symbol": "NZD/USD"},
    {"display": "EURGBP", "twelvedata_symbol": "EUR/GBP"},
    {"display": "EURJPY", "twelvedata_symbol": "EUR/JPY"},
    {"display": "EURCHF", "twelvedata_symbol": "EUR/CHF"},
    {"display": "EURCAD", "twelvedata_symbol": "EUR/CAD"},
    {"display": "EURAUD", "twelvedata_symbol": "EUR/AUD"},
    {"display": "EURNZD", "twelvedata_symbol": "EUR/NZD"},
    {"display": "GBPJPY", "twelvedata_symbol": "GBP/JPY"},
    {"display": "GBPCHF", "twelvedata_symbol": "GBP/CHF"},
    {"display": "GBPCAD", "twelvedata_symbol": "GBP/CAD"},
    {"display": "GBPAUD", "twelvedata_symbol": "GBP/AUD"},
    {"display": "GBPNZD", "twelvedata_symbol": "GBP/NZD"},
    {"display": "AUDJPY", "twelvedata_symbol": "AUD/JPY"},
    {"display": "AUDCAD", "twelvedata_symbol": "AUD/CAD"},
    {"display": "AUDCHF", "twelvedata_symbol": "AUD/CHF"},
    {"display": "AUDNZD", "twelvedata_symbol": "AUD/NZD"},
    {"display": "CADJPY", "twelvedata_symbol": "CAD/JPY"},
    {"display": "CADCHF", "twelvedata_symbol": "CAD/CHF"},
    {"display": "CHFJPY", "twelvedata_symbol": "CHF/JPY"},
    {"display": "NZDJPY", "twelvedata_symbol": "NZD/JPY"},
    {"display": "NZDCAD", "twelvedata_symbol": "NZD/CAD"},
    {"display": "NZDCHF", "twelvedata_symbol": "NZD/CHF"},
    {"display": "XAUUSD", "twelvedata_symbol": "XAU/USD"},
    {"display": "BTCUSD", "twelvedata_symbol": "BTC/USD"},
    # NAS100 removed - "VERIFY_ME" threw on every run. Add back once you
    # confirm the real Twelve Data symbol.
]

LOOKBACK = {"1week": 3, "1day": 3, "4h": 3}
OUTPUTSIZE = {"1week": 200, "1day": 250, "4h": 300}

# how long a cached series stays fresh
CACHE_TTL = {"1week": timedelta(hours=12), "1day": timedelta(hours=6)}

REQUEST_SPACING = 8  # seconds; free tier is 8 requests/minute

TWELVEDATA_API_KEY = os.environ["TWELVEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "data", "state.json")
CACHE_DIR = os.path.join(HERE, "data", "cache")
BASE_URL = "https://api.twelvedata.com/time_series"
TZ_NAME = "Africa/Lagos"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _cache_path(symbol, interval):
    safe = symbol.replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{interval}.json")


def _read_cache(symbol, interval):
    path = _cache_path(symbol, interval)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            blob = json.load(f)
        fetched = datetime.fromisoformat(blob["fetched_at"])
        if datetime.now(timezone.utc) - fetched > CACHE_TTL[interval]:
            return None
        return blob["bars"]
    except Exception:
        return None


def _write_cache(symbol, interval, bars):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(symbol, interval), "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "bars": bars}, f)


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------

def fetch_series(symbol, interval):
    """Bars OLDEST -> NEWEST, including the still-forming last bar."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": OUTPUTSIZE[interval],
        "timezone": TZ_NAME,
        "apikey": TWELVEDATA_API_KEY,
        "order": "asc",
    }
    resp = requests.get(BASE_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise RuntimeError(f"{symbol} {interval}: {data.get('message')}")
    values = data.get("values")
    minimum = 2 * LOOKBACK[interval] + 10
    if not values or len(values) < minimum:
        raise RuntimeError(f"{symbol} {interval}: only {len(values or [])} bars")
    return [{"datetime": v["datetime"],
             "high": float(v["high"]),
             "low": float(v["low"]),
             "close": float(v["close"])} for v in values]


def get_series(symbol, interval, budget):
    """Cached for weekly/daily, always fresh for 4H. `budget` counts requests."""
    if interval in CACHE_TTL:
        cached = _read_cache(symbol, interval)
        if cached is not None:
            return cached
    bars = fetch_series(symbol, interval)
    budget.append(1)
    time.sleep(REQUEST_SPACING)
    if interval in CACHE_TTL:
        _write_cache(symbol, interval, bars)
    return bars


# ---------------------------------------------------------------------------
# alerting
# ---------------------------------------------------------------------------

def fmt_price(x):
    return f"{x:.5f}" if abs(x) < 100 else f"{x:.2f}"


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                         timeout=20)
    resp.raise_for_status()


FLAG_TEXT = {
    "no_weekly_rejection": "No weekly rejection in play",
    "weekly_daily_divergence": "Weekly and daily bias DISAGREE",
    "provisional_daily": "Daily candle still forming - rejection may un-print",
}


def build_message(display, payload, run_time_str):
    arrow = "\U0001F53B" if payload["action"] == "SELL" else "\U0001F53A"
    w, d, h = payload["weekly"], payload["daily"], payload["h4"]

    lines = [
        f"{arrow} {payload['action']} {display}  [{payload['grade']}]",
        "",
        f"Weekly bias    : {w['bias'] or 'none'}",
    ]
    if w["rejection"]:
        lines.append(f"Weekly reject  : {w['rejection']['level_kind']}-shaped "
                     f"{fmt_price(w['rejection']['level_price'])} "
                     f"({w['rejection']['datetime']})")
    lines += [
        f"Daily bias     : {d['bias']} "
        f"({'aligned' if d['aligned_with_weekly'] else 'NOT aligned'})",
        f"Daily BOS      : {d['bos_datetime']}",
        f"Daily reject   : {d['rejection']['level_kind']}-shaped "
        f"{fmt_price(d['rejection']['level_price'])} ({d['rejection']['datetime']})",
        f"4H BOS         : {h['bos_datetime']} @ {fmt_price(h['bos_price'])} "
        f"(through {fmt_price(h['bos_level_price'])})",
    ]
    if payload["flags"]:
        lines += [""] + [f"\u26a0 {FLAG_TEXT.get(f, f)}" for f in payload["flags"]]
    lines += [
        "",
        "Not an entry signal - look for your entry model.",
        f"\u23f0 {run_time_str} WAT",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# per symbol
# ---------------------------------------------------------------------------

def process_symbol(entry, state, run_time_str, budget):
    display = entry["display"]
    symbol = entry["twelvedata_symbol"]

    try:
        w_bars = get_series(symbol, "1week", budget)
        d_bars = get_series(symbol, "1day", budget)
        h_bars = get_series(symbol, "4h", budget)
    except Exception as e:
        print(f"[{display}] skipped: {e}")
        return

    weekly = build_context(w_bars, "1week", LOOKBACK["1week"])
    daily = build_context(d_bars, "1day", LOOKBACK["1day"])
    h4 = build_context(h_bars, "4h", LOOKBACK["4h"])

    payload = resolve(weekly, daily, h4)
    sym_state = state.get(display) or {}
    if not isinstance(sym_state, dict):
        sym_state = {}

    if payload is None:
        print(f"[{display}] no setup "
              f"(W:{weekly.bias or '-'} D:{daily.bias or '-'})")
        state[display] = sym_state
        return

    if sym_state.get("last_signature") == payload["signature"]:
        print(f"[{display}] {payload['action']} already alerted - skipping")
        state[display] = sym_state
        return

    send_telegram(build_message(display, payload, run_time_str))
    sym_state["last_signature"] = payload["signature"]
    sym_state["last_alert_at"] = run_time_str
    state[display] = sym_state
    print(f"[{display}] ALERT {payload['action']} {payload['grade']} "
          f"flags={payload['flags']}")


def main():
    state = load_state()
    now = datetime.now(timezone.utc).astimezone(WAT)
    run_time_str = now.strftime("%d %b %Y, %I:%M %p")
    budget = []

    for entry in WATCHLIST:
        process_symbol(entry, state, run_time_str, budget)

    save_state(state)
    print(f"\n{len(budget)} API requests this run")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
