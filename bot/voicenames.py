"""Temporary name for one voice room.

`/name` renames VOICE_NAME_CHANNEL_ID while the person who ran it is sitting in
it. When they leave, the room goes back to what it was called before.

Design contract:
  * The default is whatever the channel was called before the FIRST rename —
    captured then and stored, so chained renames can't drift it. With no stored
    row the bot never touches the name, so renaming by hand in Discord sticks.
  * The revert is keyed to the person who set the name, not to the room being
    empty: they leave, it reverts, even if others are still in there. Anyone
    can rename over an active name, which transfers the revert to them.
  * A startup reconcile restores the default if they left while the bot was
    down.
  * Discord rate limits channel renames to 2 per 10 minutes, so reverts run as
    background tasks (never inline in the voice event) and re-check the room
    right before editing — if the setter came back during the wait, the revert
    is abandoned.
"""

import asyncio
import time

import discord

from .config import VOICE_NAME_CHANNEL_ID
from .database import voice_name_clear, voice_name_get, voice_name_set
from .logbus import log_error

_lock = asyncio.Lock()


def _in_channel(channel: discord.VoiceChannel, user_id: int) -> bool:
    return any(m.id == user_id for m in channel.members)


async def rename(channel: discord.VoiceChannel, name: str, by_user_id: int) -> str:
    """Apply a temporary name. Returns the message to show the caller."""
    name = " ".join(name.split())
    if not name:
        return "Give me a name."

    row = voice_name_get(channel.id)
    default = row["default_name"] if row else channel.name

    async with _lock:
        if name == channel.name:
            return f"Already called **{name}**."
        try:
            await channel.edit(name=name, reason=f"/name by {by_user_id}")
        except discord.Forbidden:
            return "I don't have permission to rename that channel."
        except Exception as e:
            log_error(f"[NAME] {channel.id} -> {name!r}: {e!r}")
            return f"Failed: {e}"

        if name == default:
            voice_name_clear(channel.id)
            return f"Back to **{default}**."

        voice_name_set(channel.id, default, name, by_user_id, int(time.time()))
        return f"Renamed to **{name}** — back to **{default}** when you leave."


async def _revert(channel: discord.VoiceChannel):
    """Restore the default, unless whoever set the name is back in the room."""
    async with _lock:
        row = voice_name_get(channel.id)
        if not row or _in_channel(channel, row["set_by"]):
            return
        default = row["default_name"]
        if channel.name != default:
            try:
                await channel.edit(name=default, reason="/name expired (setter left)")
            except Exception as e:
                log_error(f"[NAME] revert {channel.id} -> {default!r}: {e!r}")
                return
            # The edit can block for minutes on the rename rate limit; if they
            # rejoined and renamed again meanwhile, leave the new row alone.
            fresh = voice_name_get(channel.id)
            if not fresh or fresh["set_by"] != row["set_by"]:
                return
            if _in_channel(channel, row["set_by"]):
                return
        voice_name_clear(channel.id)


def _channel(bot) -> discord.VoiceChannel | None:
    if not VOICE_NAME_CHANNEL_ID:
        return None
    ch = bot.get_channel(VOICE_NAME_CHANNEL_ID)
    return ch if isinstance(ch, discord.VoiceChannel) else None


def _maybe_revert(bot):
    channel = _channel(bot)
    if channel is None:
        return
    row = voice_name_get(channel.id)
    if not row or _in_channel(channel, row["set_by"]):
        return
    bot.loop.create_task(_revert(channel))


async def on_voice_state(bot, member: discord.Member,
                         before: discord.VoiceState, after: discord.VoiceState):
    """Revert once the member who set the name is no longer in the room."""
    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None
    if before_id == after_id or before_id != VOICE_NAME_CHANNEL_ID:
        return
    row = voice_name_get(VOICE_NAME_CHANNEL_ID)
    if row and row["set_by"] == member.id:
        _maybe_revert(bot)


async def _reconcile(bot):
    """Restore the default if the setter left while we were down."""
    await bot.wait_until_ready()
    _maybe_revert(bot)


def start(bot) -> None:
    """Launch the startup reconcile once (idempotent)."""
    if getattr(bot, "_voicenames_started", False):
        return
    bot._voicenames_started = True
    bot.loop.create_task(_reconcile(bot))
