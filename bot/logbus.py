"""Record errors, and push the few that can't wait to a mods-only Discord
channel (#discord-bot-console).

`log_error(...)` records a failure at the system log's error level, where
https://logs.howling.one/discord-bot serves it. `log_critical(...)` also posts
it to Discord, which is the one channel that reaches the owner's phone when
they're away from the desk -- so it is for what needs them *now*, not for
everything that went wrong.

Both are synchronous, never await and never raise, so they are safe to call
from anywhere (including deep inside `except` blocks on hot polling paths).
All Discord I/O happens in a single background task that can never die.
"""
import asyncio
import collections

import discord

from .config import DISCORD_LOG_CHANNEL_ID

_MAXLEN = 500          # bounded buffer; oldest lines drop on overflow
_FLUSH_INTERVAL = 4    # seconds between flushes
_MAX_MSG = 1900        # leave room for the ``` code-fence wrapper (<2000)

_buffer: "collections.deque[str]" = collections.deque(maxlen=_MAXLEN)


def _log(level: int, args: tuple, to_discord: bool) -> None:
    try:
        msg = " ".join(str(a) for a in args)
        # A syslog level prefix: journald strips it and records the line at
        # that level, so logs.howling.one can filter on it. Every line of a
        # multi-line message gets it, since journald splits them.
        print("\n".join(f"<{level}>{line}" for line in msg.split("\n")))
        if to_discord:
            _buffer.append(msg)
    except Exception:
        pass


def log_error(*args) -> None:
    """Print like the original `print(...)`, recorded as an error.

    Mirrors `print`'s space-joining of multiple args. Never blocks, never raises.
    """
    _log(3, args, to_discord=False)


def log_critical(*args) -> None:
    """An error that needs the owner now: recorded AND posted to Discord."""
    _log(2, args, to_discord=True)


_ESCALATE_AFTER = 3


def log_if_persistent(count: int, *args) -> None:
    """Route a repeating failure: print() while it might be a blip, log_error()
    once it has persisted for `_ESCALATE_AFTER` consecutive occurrences.

    Callers keep their own consecutive-failure counters; this only owns the
    threshold and the routing so the policy can't drift between modules.
    """
    (log_error if count >= _ESCALATE_AFTER else print)(*args)


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
