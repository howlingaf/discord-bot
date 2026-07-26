"""Temporary voice channel names.

`/rename` renames the voice channel the caller is sitting in; the name lasts
only as long as the room is occupied. When the last human leaves, the channel
goes back to its default name.

Design contract:
  * The default is whatever the channel was called before the FIRST rename —
    captured then and stored, so chained renames can't drift the default. A
    channel nobody has /renamed has no default and is never touched, so
    renaming one by hand in Discord sticks.
  * A DB row exists only while an override is live; restoring the default
    deletes it. A startup reconcile restores any room that emptied while the
    bot was down.
  * Renaming a channel is rate limited by Discord to 2 edits per 10 minutes,
    so reverts run as background tasks (never inline in the voice event) and
    re-check the room right before editing — a rejoin during the wait cancels
    the revert.
"""

import asyncio
import time

import discord

from .database import voice_name_clear, voice_name_get, voice_name_set, voice_names_all
from .logbus import log_error

# channel_id -> lock, so a revert and a rename can't interleave on one channel.
_locks: dict[int, asyncio.Lock] = {}


def _lock(channel_id: int) -> asyncio.Lock:
    lock = _locks.get(channel_id)
    if lock is None:
        lock = _locks[channel_id] = asyncio.Lock()
    return lock


def _humans(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)


def _default_name(channel: discord.VoiceChannel) -> str | None:
    """The name this channel should carry when empty, or None if it has no
    default to restore (never renamed through us)."""
    row = voice_name_get(channel.id)
    return row["default_name"] if row else None


async def rename(channel: discord.VoiceChannel, name: str, by_user_id: int) -> str:
    """Apply a temporary name. Returns the message to show the caller."""
    name = name.strip()
    if not name:
        return "Give me a name."

    default = _default_name(channel) or channel.name

    async with _lock(channel.id):
        if name == channel.name:
            return f"Already called **{name}**."

        try:
            await channel.edit(name=name, reason=f"/rename by {by_user_id}")
        except discord.Forbidden:
            return "I don't have permission to rename that channel."
        except Exception as e:
            log_error(f"[RENAME] {channel.id} -> {name!r}: {e!r}")
            return f"Failed: {e}"

        if name == default:
            voice_name_clear(channel.id)
            return f"Restored the default name **{default}**."

        voice_name_set(channel.id, default, name, by_user_id, int(time.time()))
        return f"Renamed to **{name}** — back to **{default}** when the channel empties."


async def _revert(channel: discord.VoiceChannel):
    """Restore the default name if the room is (still) empty."""
    async with _lock(channel.id):
        default = _default_name(channel)
        if default is None or _humans(channel) > 0:
            return
        if channel.name != default:
            try:
                await channel.edit(name=default, reason="/rename expired (channel empty)")
            except Exception as e:
                log_error(f"[RENAME] revert {channel.id} -> {default!r}: {e!r}")
                return
            # The edit can block for minutes on the rename rate limit; if
            # someone rejoined and renamed again meanwhile, leave their row be.
            if _humans(channel) > 0:
                return
        voice_name_clear(channel.id)


def _maybe_revert(bot, channel_id: int | None):
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return
    if _default_name(channel) is None or _humans(channel) > 0:
        return
    bot.loop.create_task(_revert(channel))


async def on_voice_state(bot, before: discord.VoiceState, after: discord.VoiceState):
    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None
    if before_id and before_id != after_id:
        _maybe_revert(bot, before_id)


async def _reconcile(bot):
    """Restore any room that emptied (or was renamed) while we were down."""
    await bot.wait_until_ready()
    for row in voice_names_all():
        _maybe_revert(bot, row["channel_id"])


def start(bot) -> None:
    """Launch the startup reconcile once (idempotent)."""
    if getattr(bot, "_voicenames_started", False):
        return
    bot._voicenames_started = True
    bot.loop.create_task(_reconcile(bot))
