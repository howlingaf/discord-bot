"""Forward error/failure logs to a mods-only Discord channel (#discord-log).

Fully isolated from the rest of the bot: `log_error(...)` is synchronous, never
awaits and never raises, so it is safe to call from anywhere (including deep
inside `except` blocks on hot polling paths). All Discord I/O happens in a single
background task that can never die. Errors/failures only — callers decide what
gets routed here; routine/success logs stay on `print`.
"""
import asyncio
import collections

import discord

from .config import DISCORD_LOG_CHANNEL_ID

_MAXLEN = 500          # bounded buffer; oldest lines drop on overflow
_FLUSH_INTERVAL = 4    # seconds between flushes
_MAX_MSG = 1900        # leave room for the ``` code-fence wrapper (<2000)

_buffer: "collections.deque[str]" = collections.deque(maxlen=_MAXLEN)


def log_error(*args) -> None:
    """Print like the original `print(...)` AND queue the line for #discord-log.

    Mirrors `print`'s space-joining of multiple args. Never blocks, never raises.
    """
    try:
        msg = " ".join(str(a) for a in args)
        print(msg)
        _buffer.append(msg)
    except Exception:
        pass


def start(bot) -> None:
    """Launch the background flush task once (idempotent across reconnects)."""
    if getattr(bot, "_logbus_started", False):
        return
    bot._logbus_started = True
    bot.loop.create_task(_flush_loop(bot))


def _chunk(lines: list[str], max_len: int = _MAX_MSG) -> list[str]:
    """Pack lines into messages <= max_len, hard-splitting any over-long line."""
    chunks: list[str] = []
    cur = ""
    for line in lines:
        while len(line) > max_len:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        piece = line if not cur else "\n" + line
        if len(cur) + len(piece) > max_len:
            chunks.append(cur)
            cur = line
        else:
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks


async def _flush_loop(bot) -> None:
    if not DISCORD_LOG_CHANNEL_ID:
        return
    try:
        await bot.wait_until_ready()
    except Exception:
        pass
    while not bot.is_closed():
        # The whole body is guarded so the task can never die. Sends are lossy on
        # failure (dropped, not re-queued) so an outage can't grow the backlog.
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            if not _buffer:
                continue
            lines = []
            while _buffer:
                lines.append(_buffer.popleft())
            channel = bot.get_channel(DISCORD_LOG_CHANNEL_ID) or await bot.fetch_channel(DISCORD_LOG_CHANNEL_ID)
            for chunk in _chunk(lines):
                await channel.send(
                    f"```\n{chunk}\n```",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await asyncio.sleep(0.5)
        except Exception:
            continue
