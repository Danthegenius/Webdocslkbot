"""
levels.py
---------
Detection of the two key-level types, returned as a single `Level` shape so
that downstream rules never branch on how a level was found.

  A-shaped : swing high -> acts as RESISTANCE
  V-shaped : swing low  -> acts as SUPPORT

Bars are dicts ordered OLDEST -> NEWEST (index 0 = oldest):

    {"datetime": str, "high": float, "low": float, "close": float}

--------------------------------------------------------------------------
LOOKAHEAD DISCIPLINE
--------------------------------------------------------------------------
Every level carries two indices:

    index        the bar that defines the level's price
    confirmed_at the earliest bar index at which you were ALLOWED to know it

`confirmed_at = index + lookback`, because a pivot needs `lookback` bars on
its right side before it is a pivot at all.

Any query about "what was true as of bar N" must filter on `confirmed_at <= N`,
never on `index <= N`. Filtering on `index` is the single easiest way to make a
backtest look profitable and a live account not.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

SWING_LOOKBACK = 3  # bars required either side to confirm a pivot

RESISTANCE = "resistance"
SUPPORT = "support"


@dataclass(frozen=True)
class Level:
    kind: str           # "A" | "V"
    price: float
    side: str           # RESISTANCE | SUPPORT
    index: int          # bar that defines the price
    confirmed_at: int   # earliest index at which this level is knowable
    datetime: str       # datetime of `index`


def find_levels(bars: Sequence[dict], lookback: int = SWING_LOOKBACK) -> List[Level]:
    """
    Confirmed swing pivots, oldest first.

    Tiebreak: a pivot must be strictly beyond every bar to its RIGHT, and at
    least equal to every bar to its LEFT. This means an equal-high double top
    registers the SECOND high as the level rather than discarding both, which
    is what the old strict-both-sides comparison did.

    A bar can qualify as both a high and a low pivot (outside bars). Both are
    emitted -- the old implementation used `elif` and silently dropped the low.
    """
    levels: List[Level] = []
    n = len(bars)

    for i in range(lookback, n - lookback):
        left = bars[i - lookback:i]
        right = bars[i + 1:i + lookback + 1]

        h = bars[i]["high"]
        if all(h >= b["high"] for b in left) and all(h > b["high"] for b in right):
            levels.append(Level(
                kind="A", price=h, side=RESISTANCE,
                index=i, confirmed_at=i + lookback, datetime=bars[i]["datetime"],
            ))

        lo = bars[i]["low"]
        if all(lo <= b["low"] for b in left) and all(lo < b["low"] for b in right):
            levels.append(Level(
                kind="V", price=lo, side=SUPPORT,
                index=i, confirmed_at=i + lookback, datetime=bars[i]["datetime"],
            ))

    return levels


# ---------------------------------------------------------------------------
# validity
# ---------------------------------------------------------------------------

def is_invalidated(level: Level, bars: Sequence[dict], as_of: int) -> bool:
    """
    A level dies when a bar CLOSES through it, on the far side.

    Resistance dies on a close above; support dies on a close below. This is
    the weekly invalidation rule and it applies identically on every timeframe.

    Only bars from `confirmed_at` onward are considered. A bar that closed
    through the level before the level was knowable cannot have invalidated
    something that did not yet exist.
    """
    for b in bars[level.confirmed_at + 1:as_of + 1]:
        if level.side == RESISTANCE and b["close"] > level.price:
            return True
        if level.side == SUPPORT and b["close"] < level.price:
            return True
    return False


def active_levels(levels: Sequence[Level],
                  bars: Sequence[dict],
                  as_of: int) -> Tuple[Optional[Level], Optional[Level]]:
    """
    The most recent live resistance and the most recent live support as of
    bar `as_of`.

    "Live" means confirmed by `as_of` and not yet closed through. Iterating in
    reverse and stopping at the first survivor per side keeps this O(n) in the
    common case instead of the O(n^2) full scan the old version did.
    """
    live_res: Optional[Level] = None
    live_sup: Optional[Level] = None

    for lv in sorted(levels, key=lambda x: x.confirmed_at, reverse=True):
        if lv.confirmed_at > as_of:
            continue
        if lv.side == RESISTANCE and live_res is None:
            if not is_invalidated(lv, bars, as_of):
                live_res = lv
        elif lv.side == SUPPORT and live_sup is None:
            if not is_invalidated(lv, bars, as_of):
                live_sup = lv
        if live_res is not None and live_sup is not None:
            break

    return live_res, live_sup
