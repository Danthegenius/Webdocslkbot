"""
resolver.py
-----------
The W -> D -> H4 pipeline.

Runs BOTTOM-UP even though the strategy is described top-down: the trigger is
a 4H break of structure, and each higher timeframe is then asked "what was
true as of that moment". Anchoring every query to a timestamp is what makes
the lookahead bug structurally impossible instead of something you have to
remember not to write.

Flags, not gates -- per spec, the alert still fires and reports the problem:
  no_weekly_rejection      no bias-agreeing weekly rejection was live
  weekly_daily_divergence  weekly and daily bias disagree
  provisional_daily        daily rejection is on the still-forming candle
"""

from typing import Optional

from context import TimeframeContext, rejection_on, bar_close_dt, to_dt
from levels import RESISTANCE, SUPPORT
from rules import (structural_bos, origin_swing, setup_bos, action_for,
                   BULLISH, BEARISH, BUY, SELL)


def _h4_bos_after(h4: TimeframeContext, since, direction) -> Optional[dict]:
    """
    A 4H break of structure, in `direction`, on a bar that closed after
    `since`.

    This is a 4H-SCALE break: a close through the most recent confirmed 4H
    level. The old scanner passed the DAILY origin swing here, which meant a
    4H candle had to close 1-5% beyond the rejection before anything fired --
    the reason setups sat pending until they expired.
    """
    for i in range(h4.as_of, -1, -1):
        if bar_close_dt(h4.bars[i], "4h") <= since:
            break
        bos = structural_bos(h4.bars, h4.levels, i)
        if not bos or bos["index"] != i:
            continue
        want = BULLISH if direction == BUY else BEARISH
        if bos["direction"] == want:
            return {
                "index": i,
                "datetime": h4.bars[i]["datetime"],
                "close_dt": bar_close_dt(h4.bars[i], "4h"),
                "price": h4.bars[i]["close"],
                "level": bos["level"],
            }
    return None


def resolve(weekly: TimeframeContext,
            daily: TimeframeContext,
            h4: TimeframeContext) -> Optional[dict]:
    """
    Returns an alert payload, or None if no setup exists.
    """
    flags = []

    # ---- weekly -----------------------------------------------------------
    w_rej = rejection_on(weekly)
    if w_rej is None:
        flags.append("no_weekly_rejection")

    # ---- daily ------------------------------------------------------------
    if daily.bias is None:
        return None
    if weekly.bias is not None and weekly.bias != daily.bias:
        flags.append("weekly_daily_divergence")

    # the daily BOS that set the bias must come after the weekly rejection
    if w_rej is not None and daily.bias_bos is not None:
        d_bos_dt = bar_close_dt(daily.bars[daily.bias_bos["index"]], "1day")
        if d_bos_dt <= w_rej["close_dt"]:
            return None

    # branch 2 allows the still-forming daily candle; it is flagged, not gated
    d_rej = rejection_on(daily, include_forming=True)
    if d_rej is None:
        return None
    if d_rej["provisional"]:
        flags.append("provisional_daily")

    # the daily rejection must come after the daily BOS that set the bias
    if daily.bias_bos is not None:
        if d_rej["close_dt"] <= bar_close_dt(daily.bars[daily.bias_bos["index"]], "1day"):
            return None

    direction = action_for(d_rej["level"])

    # ---- has the daily setup already died? --------------------------------
    if d_rej["index"] is not None:
        origin = origin_swing(daily.levels, d_rej["level"])
        if origin is not None:
            status = setup_bos(daily.bars, d_rej, origin, daily.as_of)
            if status["status"] == "invalidated":
                return None

    # ---- 4H ---------------------------------------------------------------
    h4_bos = _h4_bos_after(h4, d_rej["close_dt"], direction)
    if h4_bos is None:
        return None

    # ---- grading ----------------------------------------------------------
    # A+ = the daily candle had already closed when the 4H BOS fired.
    day_closed = (not d_rej["provisional"]) and h4_bos["close_dt"] >= d_rej["close_dt"]
    grade = "A+" if day_closed else "standard"

    return {
        "action": direction,
        "grade": grade,
        "flags": flags,
        "weekly": {
            "bias": weekly.bias,
            "rejection": None if w_rej is None else {
                "level_kind": w_rej["level"].kind,
                "level_price": w_rej["level"].price,
                "datetime": w_rej["datetime"],
            },
        },
        "daily": {
            "bias": daily.bias,
            "aligned_with_weekly": weekly.bias == daily.bias,
            "bos_datetime": None if daily.bias_bos is None else daily.bias_bos["datetime"],
            "rejection": {
                "level_kind": d_rej["level"].kind,
                "level_price": d_rej["level"].price,
                "datetime": d_rej["datetime"],
                "provisional": d_rej["provisional"],
            },
        },
        "h4": {
            "bos_datetime": h4_bos["datetime"],
            "bos_price": h4_bos["price"],
            "bos_level_price": h4_bos["level"].price,
        },
        # dedupe key: one alert per (daily rejection, 4H BOS) pair
        "signature": f"{d_rej['datetime']}|{h4_bos['datetime']}|{direction}",
    }
