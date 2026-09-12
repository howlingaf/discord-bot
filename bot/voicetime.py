"""Time spent in voice: every user, every voice channel.

One row per user per channel stint in `voice_visits`, for everyone (staff
included), so time is attributed to the room it was actually spent in and any
total is an aggregate in SQL. Nothing is decided from it — it's a record, read
by the host's card in the admin channel and by scripts/voice_time.py.

This used to sit under a "fair-access" system that hid a newcomer room from
regulars once they passed a threshold. That's gone; the record it kept wasn't.
"""

import asyncio
import time
from datetime import datetime, timedelta

import discord

from .config import (
    ADMIN_PANEL_CHANNEL_ID,
    STREAMER_DISCORD_ID,
    VOICE_TIME_HOST_ROOMS,
)
from .database import (
    fairaccess_set_panel_message,
    fairaccess_state_get,
    heartbeat_get,
    heartbeat_set,
    voice_visit_close,
    voice_visit_open_for,
    voice_visit_recent_same_channel,
    voice_visit_resume,
    voice_visit_start,
    voice_visits_open,
    voice_visits_since,
)
from .logbus import log_error, log_if_persistent

# Refreshes the heartbeat a restart credits open visits up to, and keeps the
# host card's "this week" current while nobody is moving between channels.
_TICK_SECONDS = 300
# Rejoining the same voice channel within this gap resumes the visit row.
_VISIT_MERGE_SECONDS = 300

_lock = asyncio.Lock()
_render_lock = asyncio.Lock()


def _now() -> int:
    return int(time.time())


# ------------------------------------------------------------------ #
#  Voice events                                                      #
# ------------------------------------------------------------------ #

async def on_voice_state(bot, member: discord.Member,
                         before: discord.VoiceState, after: discord.VoiceState) -> None:
    """Entry point from events.py. Never raises."""
    try:
        if member.bot:
            return
        b = before.channel.id if before.channel else None
        a = after.channel.id if after.channel else None
        if b == a:
            return  # mute/deafen/stream toggles fire this event too
        async with _lock:
            _visit_transition(member.id, b, a, _now())
        # Only the host's time is on the card, so only their moves re-render it.
        if member.id == STREAMER_DISCORD_ID:
            await render_panel(bot)
    except Exception as e:
        log_error(f"[VOICETIME] voice handler failed: {e!r}")


def _visit_transition(user_id: int, left_cid: int | None,
                      joined_cid: int | None, now: int) -> None:
    """Close the row for the channel left, open (or resume) one for the channel
    joined. Moving channels is both; a quick rejoin resumes rather than
    fragmenting the stint into pieces."""
    if left_cid is not None:
        v = voice_visit_open_for(user_id)
        if v and v["channel_id"] == left_cid:
            voice_visit_close(v["id"], now)
    if joined_cid is not None and voice_visit_open_for(user_id) is None:
        merge = voice_visit_recent_same_channel(
            user_id, joined_cid, now - _VISIT_MERGE_SECONDS)
        if merge is not None:
            voice_visit_resume(merge, now)
        else:
            voice_visit_start(user_id, joined_cid, now)


# ------------------------------------------------------------------ #
#  Host card (one pinned message in the admin channel)               #
# ------------------------------------------------------------------ #

def _week_start(ts: int) -> int:
    """Unix time of the Monday 00:00 opening `ts`'s week, server local time."""
    d = datetime.fromtimestamp(ts)
    monday = (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _host_time_rows(user_id: int, rooms: list[int], now: int) -> list[tuple[str, int]]:
    """[(label, seconds)] for this month, this week and last week.

    A visit counts toward the period it STARTED in — one running through
    Sunday midnight lands wholly in the earlier week. One query covers all
    three periods; each row is a filtered sum over the same visits.
    """
    this_wk = _week_start(now)
    last_wk = this_wk - 7 * 86400
    month = int(datetime.fromtimestamp(now).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    visits = voice_visits_since(user_id, rooms, min(month, last_wk)) if rooms else []

    def total(since: int, until: int | None = None) -> int:
        return sum(v["seconds"] + (max(0, now - v["last_join_at"]) if v["left_at"] is None else 0)
                   for v in visits if v["started_at"] >= since
                   and (until is None or v["started_at"] < until))

    return [(f"{datetime.fromtimestamp(now):%B}", total(month)),
            ("this week", total(this_wk)),
            ("last week", total(last_wk, this_wk))]


def _room_name(bot, channel_id: int) -> str:
    ch = bot.get_channel(channel_id)
    return ch.name if ch else str(channel_id)


def _build_panel(bot) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if not STREAMER_DISCORD_ID:
        return view
    rows = _host_time_rows(STREAMER_DISCORD_ID, VOICE_TIME_HOST_ROOMS, _now())
    # ansi block: the only way to colour individual lines. Month yellow,
    # this week bold green, last week cyan.
    body = "```ansi\n" + "\n".join(
        f"\u001b[{colour}m{label:<12}{_hm(secs)}\u001b[0m"
        for (label, secs), colour in zip(rows, ("33", "1;32", "36"))) + "\n```"
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"### <@{STREAMER_DISCORD_ID}>"),
        discord.ui.TextDisplay(body),
        discord.ui.TextDisplay(
            "-# " + " · ".join(f"#{_room_name(bot, c)}" for c in VOICE_TIME_HOST_ROOMS)),
        accent_color=0xFAA61A))
    return view


async def render_panel(bot) -> None:
    """Re-render the pinned card in place; recreate it once if it was deleted."""
    async with _render_lock:
        try:
            channel = (bot.get_channel(ADMIN_PANEL_CHANNEL_ID)
                       or await bot.fetch_channel(ADMIN_PANEL_CHANNEL_ID))
            view = _build_panel(bot)
            mid = fairaccess_state_get()["panel_message_id"]
            if mid:
                try:
                    await channel.get_partial_message(mid).edit(view=view)
                    return
                except discord.NotFound:
                    pass  # deleted — recreate below
            msg = await channel.send(view=view)
            fairaccess_set_panel_message(msg.id)
            try:
                await msg.pin()
            except Exception as e:
                print(f"[VOICETIME] could not pin panel: {e}")
        except Exception as e:
            log_error(f"[VOICETIME] panel render failed: {e!r}")


# ------------------------------------------------------------------ #
#  Startup + tick                                                    #
# ------------------------------------------------------------------ #

def _startup_fixups(bot, now: int) -> None:
    """A restart must not cost anyone the time they were sitting on. A visit
    left open while the bot was down stays open if they're still in that exact
    channel (the whole span counts); otherwise it's credited up to the last
    heartbeat — the last moment we know they were connected."""
    beat = heartbeat_get()
    for v in voice_visits_open():
        ch = bot.get_channel(v["channel_id"])
        still_there = ch is not None and any(
            m.id == v["user_id"] for m in getattr(ch, "members", []))
        if not still_there:
            voice_visit_close(v["id"], max(v["last_join_at"], min(beat or now, now)))


async def _loop(bot) -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    async with _lock:
        _startup_fixups(bot, _now())
        heartbeat_set(_now())
    await render_panel(bot)
    print("✅ Voice time tracking started (all voice channels)")

    fails = 0
    while not bot.is_closed():
        await asyncio.sleep(_TICK_SECONDS)
        try:
            heartbeat_set(_now())
            await render_panel(bot)
            fails = 0
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[VOICETIME] tick failed (attempt {fails}): {e!r}")


def start(bot) -> None:
    """Launch the tick loop once (idempotent)."""
    if getattr(bot, "_voicetime_started", False):
        return
    bot._voicetime_started = True
    bot.loop.create_task(_loop(bot))
