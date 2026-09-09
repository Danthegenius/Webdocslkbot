"""
context.py
----------
Resolves "what was true on this timeframe, as of this bar" into a single
object. One instance per timeframe per symbol per run.

The whole point is that nothing downstream ever indexes into `bars` directly,
so nothing downstream can accidentally read a bar that had not printed yet.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from levels import Level, find_levels, active_levels, SWING_LOOKBACK, RESISTANCE, SUPPORT
from rules import (structural_bos, agrees_with_bias, find_rejection,
                   origin_swing, setup_bos, action_for, BULLISH, BEARISH, BUY, SELL)


def to_dt(value: str) -> datetime:
    """
    Twelve Data returns '2026-09-08' for 1day/1week and
    '2026-09-08 12:00:00' for 4h. Normalise both to datetime.
    """
    value = value.strip()
    if len(value) == 10:
        return datetime.strptime(value, "%Y-%m-%d")
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def bar_close_dt(bar: dict, interval: str) -> datetime:
    """
    When this bar CLOSED. Twelve Data timestamps a bar by its OPEN, so the
    close is one interval later.

    NOTE: forex daily bars close at the session boundary (5pm EST), not at
    midnight, and weekly bars close Friday. Adding a flat 1 day / 7 days is
    an approximation good enough for ordering 4H bars against a daily close,
    but verify it against real timestamps before trusting the A+ grading.
    """
    start = to_dt(bar["datetime"])
    if interval == "1week":
        return start + timedelta(days=7)
    if interval == "1day":
        return start + timedelta(days=1)
    if interval == "4h":
        return start + timedelta(hours=4)
    raise ValueError(f"unknown interval {interval}")


@dataclass
class TimeframeContext:
    interval: str
    bars: List[dict]
    levels: List[Level]
    as_of: int                      # index of the last CLOSED bar
    forming: Optional[dict]         # the in-progress bar, or None
    bias: Optional[str]
    bias_bos: Optional[dict]

    @property
    def last_closed(self) -> dict:
        return self.bars[self.as_of]

    def live_levels(self):
        return active_levels(self.levels, self.bars, self.as_of)

    def bias_level(self) -> Optional[Level]:
        """The live level that a bias-agreeing rejection would come from."""
        res, sup = self.live_levels()
        if self.bias == BULLISH:
            return sup
        if self.bias == BEARISH:
            return res
        return None

    def direction(self) -> Optional[str]:
        if self.bias == BULLISH:
            return BUY
        if self.bias == BEARISH:
            return SELL
        return None


def build_context(bars: Sequence[dict],
                  interval: str,
                  lookback: int = SWING_LOOKBACK,
                  drop_forming: bool = True) -> TimeframeContext:
    """
    `bars` comes straight from the feed, oldest -> newest, with the last
    element being the in-progress candle.

    We split it off rather than deleting it: the daily timeframe needs it for
    the branch-2 rule ("don't wait for the day to close"), but no level
    detection, bias or invalidation logic may ever see it.
    """
    bars = list(bars)
    forming = bars.pop() if (drop_forming and bars) else None

    levels = find_levels(bars, lookback)
    as_of = len(bars) - 1
    bos = structural_bos(bars, levels, as_of)

    return TimeframeContext(
        interval=interval,
        bars=bars,
        levels=levels,
        as_of=as_of,
        forming=forming,
        bias=bos["direction"] if bos else None,
        bias_bos=bos,
    )


def rejection_on(ctx: TimeframeContext,
                 since: Optional[datetime] = None,
                 include_forming: bool = False) -> Optional[dict]:
    """
    The most recent bias-agreeing rejection off the live level, optionally
    restricted to bars that closed after `since`.

    Returns a dict with an added "provisional" flag: True when the rejection
    is on the still-forming candle and could un-print. Everything downstream
    must carry that flag through to the alert.
    """
    level = ctx.bias_level()
    if level is None or ctx.bias is None:
        return None
    if not agrees_with_bias(level, ctx.bias):
        return None

    since_idx = None
    if since is not None:
        for i in range(ctx.as_of, -1, -1):
            if bar_close_dt(ctx.bars[i], ctx.interval) <= since:
                since_idx = i
                break

    rej = find_rejection(ctx.bars, level, ctx.as_of, since=since_idx)
    if rej:
        rej["provisional"] = False
        rej["close_dt"] = bar_close_dt(ctx.bars[rej["index"]], ctx.interval)
        return rej

    # branch 2: allow the in-progress candle, clearly marked
    if include_forming and ctx.forming is not None:
        from rules import is_rejection
        if is_rejection(ctx.forming, level):
            return {
                "index": None,
                "datetime": ctx.forming["datetime"],
                "price": level.price,
                "level": level,
                "provisional": True,
                "close_dt": bar_close_dt(ctx.forming, ctx.interval),
            }
    return None
