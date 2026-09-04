"""
structure.py
------------
Implements the trade-structure rules:

  1. A keylevel is a swing high ("A" shape) or swing low ("V" shape) —
     a candle whose high/low exceeds SWING_LOOKBACK candles on each side.
  2. A rejection happens when a candle taps a keylevel (wicks beyond it)
     and CLOSES back on the near side.
  3. The "origin swing" is the most recent opposite-type swing point
     BEFORE the keylevel — i.e. where the leg that carried price into
     the keylevel actually started.
  4. A BOS (break of structure) is a later candle's body CLOSING beyond
     that origin swing, in the direction of the rejection.

Bars are plain dicts: {"datetime": str, "high": float, "low": float, "close": float}
ordered OLDEST -> NEWEST (index 0 = oldest).
"""

SWING_LOOKBACK = 3  # candles required on each side to confirm a pivot


def find_swings(bars, lookback=SWING_LOOKBACK):
    """
    Returns a list of confirmed swing points:
    [{"index": i, "type": "high"|"low", "price": float, "datetime": str}, ...]
    A bar needs `lookback` candles on BOTH sides to be confirmed, so the most
    recent `lookback` bars can never produce a swing yet (not enough future
    data to confirm them) — that's expected and correct.
    """
    swings = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        window = bars[i - lookback: i + lookback + 1]
        this_high = bars[i]["high"]
        this_low = bars[i]["low"]

        if all(this_high >= b["high"] for b in window) and \
           this_high > max(b["high"] for j, b in enumerate(window) if j != lookback):
            swings.append({"index": i, "type": "high", "price": this_high, "datetime": bars[i]["datetime"]})

        elif all(this_low <= b["low"] for b in window) and \
                this_low < min(b["low"] for j, b in enumerate(window) if j != lookback):
            swings.append({"index": i, "type": "low", "price": this_low, "datetime": bars[i]["datetime"]})

    return swings


def active_keylevels(bars, swings):
    """
    From all confirmed swings, return the most recent swing high and the
    most recent swing low that have NOT since been closed-through (i.e.
    still "live" as a keylevel, not already invalidated by a later BOS).
    """
    latest_high = None
    latest_low = None

    for s in swings:
        if s["type"] == "high":
            # invalidated if any later bar already closed above it
            broken = any(b["close"] > s["price"] for b in bars[s["index"] + 1:])
            if not broken:
                latest_high = s
        else:
            broken = any(b["close"] < s["price"] for b in bars[s["index"] + 1:])
            if not broken:
                latest_low = s

    return latest_high, latest_low


def origin_swing(swings, keylevel):
    """
    The most recent swing of the OPPOSITE type before the keylevel's index —
    i.e. where the leg into the keylevel began.
    """
    opposite = "low" if keylevel["type"] == "high" else "high"
    candidates = [s for s in swings if s["type"] == opposite and s["index"] < keylevel["index"]]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s["index"])


def check_rejection(bars, keylevel):
    """
    Checks ONLY the most recently closed bar for a fresh tap-and-reject
    against the given keylevel. Returns the rejection bar (dict) or None.
    """
    if not bars:
        return None
    last = bars[-1]

    if keylevel["type"] == "high":
        tapped = last["high"] >= keylevel["price"]
        rejected = last["close"] < keylevel["price"]
        if tapped and rejected:
            return last
    else:
        tapped = last["low"] <= keylevel["price"]
        rejected = last["close"] > keylevel["price"]
        if tapped and rejected:
            return last

    return None


def check_bos(bars_after, origin, direction):
    """
    Scans bars_after (chronological, all strictly after the rejection) for
    the first candle whose BODY CLOSE breaks beyond the origin swing price,
    in the given direction ("SELL" = close below origin low,
    "BUY" = close above origin high).
    Returns that bar (dict) or None if not yet broken.
    """
    for b in bars_after:
        if direction == "SELL" and b["close"] < origin["price"]:
            return b
        if direction == "BUY" and b["close"] > origin["price"]:
            return b
    return None
