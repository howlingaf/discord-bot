"""Relay logs the Twitch bot pushes into the #twitch-bot-console channel.

Fed by the inbound POST /twitch-log endpoint (web.py). Buffers incoming lines and
flushes them batched to TWITCH_CONSOLE_CHANNEL_ID every few seconds, so the Twitch
bot can push as often as it likes without hitting Discord rate limits. Isolated
like logbus: push() never raises and the flush task can never die.
"""
import asyncio
import collections

import discord

from .config import TWITCH_CONSOLE_CHANNEL_ID
from .logbus import _chunk

_MAXLEN = 1000
_FLUSH_INTERVAL = 4

_buffer: "collections.deque[str]" = collections.deque(maxlen=_MAXLEN)


def push(lines) -> None:
    """Queue one line (str) or many (list[str]) for #twitch-bot-console. Never raises."""
    try:
        if isinstance(lines, str):
            lines = [lines]
        for ln in lines:
            _buffer.append(str(ln))
    except Exception:
        pass


def start(bot) -> None:
    """Launch the background flush task once (idempotent across reconnects)."""
    if getattr(bot, "_twitchlog_started", False):
        return
    bot._twitchlog_started = True
    bot.loop.create_task(_flush_loop(bot))


async def _flush_loop(bot) -> None:
    if not TWITCH_CONSOLE_CHANNEL_ID:
        return
    try:
        await bot.wait_until_ready()
    except Exception:
        pass
    while not bot.is_closed():
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            if not _buffer:
                continue
            lines = []
            while _buffer:
                lines.append(_buffer.popleft())
            channel = bot.get_channel(TWITCH_CONSOLE_CHANNEL_ID) or await bot.fetch_channel(TWITCH_CONSOLE_CHANNEL_ID)
            for chunk in _chunk(lines):
                await channel.send(
                    f"```\n{chunk}\n```",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await asyncio.sleep(0.5)
        except Exception:
            continue
