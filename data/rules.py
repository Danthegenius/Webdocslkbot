"""
rules.py
--------
Pure predicates over bars and Levels. Nothing here fetches data, holds state,
or decides whether a setup is tradeable -- it only answers factual questions
about what the chart did.

Every function takes an explicit `as_of` bar index where it could otherwise
see into the future.

--------------------------------------------------------------------------
TWO DIFFERENT THINGS ARE BOTH CALLED "BOS"
--------------------------------------------------------------------------
Your spec uses the word for both, and they are not the same rule:

  structural_bos()  Establishes BIAS. A close through the most recent
                    confirmed key level. Needs no rejection first.
                    This is what Step 1 and the daily/4H "confirm a BOS"
                    instructions mean.

  setup_bos()       CONFIRMS a specific rejection. A close beyond the origin
                    swing of the leg that ran into the rejected level, and it
                    races against invalidation -- if price closes back through
                    the rejected level first, the setup is dead and no BOS
                    can be claimed. This is what the old check_bos() did,
                    minus the invalidation race.
"""

from typing import Optional, Sequence

from levels import Level, RESISTANCE, SUPPORT

BUY = "BUY"
SELL = "SELL"

BULLISH = "bullish"
BEARISH = "bearish"


# ---------------------------------------------------------------------------
# bias
# ---------------------------------------------------------------------------

def structural_bos(bars: Sequence[dict],
                   levels: Sequence[Level],
                   as_of: int) -> Optional[dict]:
    """
    The most recent break of structure as of `as_of`.

    Walks backwards from `as_of`. For each bar, asks whether that bar closed
    through any level that was already confirmed at the time. The first such
    bar found going backwards is the latest BOS.

    Returns {"index", "datetime", "direction", "level"} or None.
    """
    for i in range(as_of, -1, -1):
        bar = bars[i]
        for lv in levels:
            if lv.confirmed_at >= i:
                continue  # not knowable when this bar closed
            if lv.side == RESISTANCE and bar["close"] > lv.price:
                return {"index": i, "datetime": bar["datetime"],
                        "direction": BULLISH, "level": lv}
            if lv.side == SUPPORT and bar["close"] < lv.price:
                return {"index": i, "datetime": bar["datetime"],
                        "direction": BEARISH, "level": lv}
    return None


def bias_of(bars, levels, as_of) -> Optional[str]:
    """BULLISH / BEARISH / None, from the most recent structural BOS."""
    bos = structural_bos(bars, levels, as_of)
    return bos["direction"] if bos else None


def agrees_with_bias(level: Level, bias: str) -> bool:
    """
    A rejection is only tradeable if it points the same way as the bias.

    Bullish bias -> only V-shaped (SUPPORT) rejections, i.e. buys.
    Bearish bias -> only A-shaped (RESISTANCE) rejections, i.e. sells.

    A counter-bias rejection is discarded outright; it does not flip the bias.
    """
    if bias == BULLISH:
        return level.side == SUPPORT
    if bias == BEARISH:
        return level.side == RESISTANCE
    return False


def action_for(level: Level) -> str:
    return BUY if level.side == SUPPORT else SELL


# ---------------------------------------------------------------------------
# rejection
# ---------------------------------------------------------------------------

def is_rejection(bar: dict, level: Level) -> bool:
    """
    The bar wicked through the level and CLOSED back on the near side.

    Support    : low  <= price and close > price
    Resistance : high >= price and close < price
    """
    if level.side == SUPPORT:
        return bar["low"] <= level.price and bar["close"] > level.price
    return bar["high"] >= level.price and bar["close"] < level.price


def find_rejection(bars: Sequence[dict],
                   level: Level,
                   as_of: int,
                   since: Optional[int] = None) -> Optional[dict]:
    """
    The most recent rejection of `level` at or before `as_of`, optionally
    constrained to bars after `since` (used to enforce "the daily rejection
    must come after the weekly rejection").

    Returns {"index", "datetime", "price", "level"} or None.
    """
    start = max(level.confirmed_at, (since + 1) if since is not None else 0)
    for i in range(as_of, start - 1, -1):
        if is_rejection(bars[i], level):
            return {"index": i, "datetime": bars[i]["datetime"],
                    "price": level.price, "level": level}
    return None


# ---------------------------------------------------------------------------
# origin swing + setup BOS
# ---------------------------------------------------------------------------

def origin_swing(levels: Sequence[Level], level: Level) -> Optional[Level]:
    """
    Where the leg that ran into `level` began: the most recent OPPOSITE-side
    level before it.
    """
    want = SUPPORT if level.side == RESISTANCE else RESISTANCE
    candidates = [lv for lv in levels if lv.side == want and lv.index < level.index]
    return max(candidates, key=lambda lv: lv.index) if candidates else None


def setup_bos(bars: Sequence[dict],
              rejection: dict,
              origin: Level,
              as_of: int) -> dict:
    """
    Scan forward from the bar after the rejection for whichever comes FIRST:

      - a close beyond `origin` in the rejection's direction  -> confirmed
      - a close back through the rejected level               -> invalidated

    The old check_bos() only looked for the first case, which meant a setup
    that had already failed hours earlier could still fire a signal later.

    Returns {"status": "confirmed"|"invalidated"|"pending", "index", "datetime",
             "price"}.
    """
    level = rejection["level"]
    direction = action_for(level)

    for i in range(rejection["index"] + 1, as_of + 1):
        bar = bars[i]

        # invalidation first: a setup that failed cannot later succeed
        if level.side == SUPPORT and bar["close"] < level.price:
            return {"status": "invalidated", "index": i,
                    "datetime": bar["datetime"], "price": bar["close"]}
        if level.side == RESISTANCE and bar["close"] > level.price:
            return {"status": "invalidated", "index": i,
                    "datetime": bar["datetime"], "price": bar["close"]}

        if direction == BUY and bar["close"] > origin.price:
            return {"status": "confirmed", "index": i,
                    "datetime": bar["datetime"], "price": bar["close"]}
        if direction == SELL and bar["close"] < origin.price:
            return {"status": "confirmed", "index": i,
                    "datetime": bar["datetime"], "price": bar["close"]}

    return {"status": "pending", "index": None, "datetime": None, "price": None}
