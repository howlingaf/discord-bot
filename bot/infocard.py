"""Keep the #info embed's "usually around <time>" honest across DST.

A Discord timestamp renders in each viewer's own timezone, which is what makes
it useful in a server spanning time zones — but it is a fixed instant, not a
wall-clock rule. "10pm Chicago" is a different instant in January than in
July, so a timestamp written once drifts an hour when DST flips. This
recomputes today's instant for that wall-clock time once a day and edits the
embed only when the rendered time would actually change.
"""

import asyncio
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import (
    INFO_CARDS,
    COWORK_USUAL_HOUR,
    COWORK_TZ,
)
from .logbus import log_error

_STAMP_RE = re.compile(r"(usually weekdays around )<t:(\d+):t>", re.I)


def _todays_instant() -> int:
    tz = ZoneInfo(COWORK_TZ)
    today = datetime.now(tz).date()
    return int(datetime.combine(today, time(COWORK_USUAL_HOUR, 0), tzinfo=tz).timestamp())


def _drifted(old_ts: int) -> bool:
    """True when DST state has changed since the stamp was written.

    A Chicago viewer always sees 10 PM — Discord converts the instant using the
    offset in force on ITS date, not today's. The drift is for everyone else:
    a viewer in a zone that doesn't move with Chicago (Tokyo, Phoenix) sees a
    winter stamp an hour off from a summer one. Re-stamp when the offset that
    was in force then differs from the one in force today — twice a year.
    """
    tz = ZoneInfo(COWORK_TZ)
    return (datetime.fromtimestamp(old_ts, tz).utcoffset()
            != datetime.fromtimestamp(_todays_instant(), tz).utcoffset())


async def _refresh_one(bot, channel_id: int, message_id: int) -> bool:
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    msg = await channel.fetch_message(message_id)
    changed = False
    embeds = []
    for e in msg.embeds:
        d = e.description or ""
        m = _STAMP_RE.search(d)
        if m and _drifted(int(m.group(2))):
            e.description = _STAMP_RE.sub(rf"\g<1><t:{_todays_instant()}:t>", d, count=1)
            changed = True
        embeds.append(e)
    if changed:
        await msg.edit(embeds=embeds)
        print(f"[INFOCARD] re-stamped usual time on {message_id}")
    return changed


async def refresh(bot) -> bool:
    """Re-stamp every card whose rendered time has drifted. Returns whether any edited.

    Each card is independent: one failing (deleted message, missing channel)
    must not stop the others from being refreshed.
    """
    any_changed = False
    for channel_id, message_id in INFO_CARDS:
        try:
            any_changed |= await _refresh_one(bot, channel_id, message_id)
        except Exception as e:
            log_error(f"[INFOCARD] refresh of {channel_id}/{message_id} failed: {e!r}")
    return any_changed


async def scheduler(bot):
    await bot.wait_until_ready()
    tz = ZoneInfo(COWORK_TZ)
    while not bot.is_closed():
        await refresh(bot)
        # Just after local midnight, when "today" changes.
        now = datetime.now(tz)
        nxt = datetime.combine(now.date() + timedelta(days=1), time(0, 5), tzinfo=tz)
        await asyncio.sleep(max(60, (nxt - now).total_seconds()))
