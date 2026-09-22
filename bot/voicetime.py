"""Time spent in voice: every user, every voice channel.

One row per user per channel stint in `voice_visits`, for everyone (staff
included), so time is attributed to the room it was actually spent in and any
total is an aggregate in SQL. Nothing is decided from it -- it's a record.

The reporting lives on the website (streaming-analytics reads this database).
So besides the visits, this keeps `directory` current: the names of members
and voice channels, so the website can label ids without a Discord token.

This used to sit under a "fair-access" system that hid a newcomer room from
regulars once they passed a threshold, and later drew the host's time as a
card in the admin channel. Both are gone; the record they kept isn't.
"""

import asyncio
import time

import discord

from .database import (
    directory_upsert,
    heartbeat_get,
    heartbeat_set,
    voice_visit_close,
    voice_visit_open_for,
    voice_visit_recent_same_channel,
    voice_visit_resume,
    voice_visit_start,
    voice_visit_user_ids,
    voice_visits_open,
)
from .logbus import log_error, log_if_persistent

# Refreshes the heartbeat a restart credits open visits up to.
_TICK_SECONDS = 300
# Rejoining the same voice channel within this gap resumes the visit row.
_VISIT_MERGE_SECONDS = 300
# Names change rarely; resync them this often in case an event was missed.
_DIRECTORY_SECONDS = 6 * 3600

_lock = asyncio.Lock()


def _now() -> int:
    return int(time.time())


def _display(member: discord.abc.User) -> str:
    return getattr(member, "display_name", None) or member.name


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
        rows = [("member", member.id, _display(member))]
        if after.channel:
            rows.append(("channel", after.channel.id, after.channel.name))
        directory_upsert(rows, _now())
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
#  Directory                                                         #
# ------------------------------------------------------------------ #

async def _sync_directory(bot) -> None:
    """Every voice channel, and everyone who has ever had a visit recorded --
    including people who've since left the server, looked up individually."""
    rows = []
    for guild in bot.guilds:
        rows += [("channel", c.id, c.name) for c in guild.voice_channels + guild.stage_channels]
    for uid in voice_visit_user_ids():
        m = next((g.get_member(uid) for g in bot.guilds if g.get_member(uid)), None)
        if m is None:
            try:
                m = await bot.fetch_user(uid)
            except discord.HTTPException:
                continue
        rows.append(("member", uid, _display(m)))
    directory_upsert(rows, _now())


# ------------------------------------------------------------------ #
#  Startup + tick                                                    #
# ------------------------------------------------------------------ #

def _startup_fixups(bot, now: int) -> None:
    """A restart must not cost anyone the time they were sitting on. A visit
    left open while the bot was down stays open if they're still in that exact
    channel (the whole span counts); otherwise it's credited up to the last
    heartbeat -- the last moment we know they were connected."""
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
    print("✅ Voice time tracking started (all voice channels)")

    fails, synced = 0, 0.0
    while not bot.is_closed():
        try:
            heartbeat_set(_now())
            if time.time() - synced >= _DIRECTORY_SECONDS:
                await _sync_directory(bot)
                synced = time.time()
            fails = 0
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[VOICETIME] tick failed (attempt {fails}): {e!r}")
        await asyncio.sleep(_TICK_SECONDS)


def start(bot) -> None:
    """Launch the tick loop once (idempotent)."""
    if getattr(bot, "_voicetime_started", False):
        return
    bot._voicetime_started = True
    bot.loop.create_task(_loop(bot))
