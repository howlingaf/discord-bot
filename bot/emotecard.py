"""A card in the admin channel: where our Twitch emotes get used elsewhere.

streaming-analytics reads ~200 other channels' chat anonymously and records
every message carrying a `howlin67` emote (analytics/emotewatch.py there). That record is
only useful if it's in front of the owner, so this renders it as one embed,
edited in place once a day rather than posted anew — the channel stays a set
of standing cards, not a feed.

The data lives in streaming-analytics' analytics.db on the same box. This opens it
read-only: nothing here writes to it, and a missing or locked file leaves the
card as it was rather than failing the tick.
"""

import asyncio
import sqlite3
import time
from datetime import date, datetime

import discord

from .config import (EMOTE_CARD_CHANNEL_ID, EMOTE_CARD_DAYS, EMOTE_PREFIX,
                     ANALYTICS_DB, STREAMER_NAME)
from .database import panel_message_get, panel_message_set
from .logbus import log_error, log_if_persistent

_PANEL = "emote_sightings"
# Checked often, drawn once a day: the tick is cheap and this way the card
# still appears promptly after a restart or a deploy.
_TICK_SECONDS = 1800
_MAX_ROWS = 15
# Wider than this and the grid stops fitting a phone screen.
_MAX_COLS = 6

_lock = asyncio.Lock()
_drawn_on: date | None = None


def _grid(days: int) -> tuple[list[str], list[tuple[str, dict[str, int], int]], int, list[str]]:
    """(emote columns, [(viewer, {emote: n}, total)], total uses, channels).

    Only emotes anyone actually used become columns — all 18 of ours would
    make a grid too wide to read on a phone, and mostly zeroes.
    """
    since = int(time.time()) - days * 86400
    try:
        db = sqlite3.connect(f"file:{ANALYTICS_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute(
                # The owner's own uses aren't the audience's; never count them.
                "SELECT login, emote, COUNT(*) FROM emote_sightings WHERE ts >= ? AND login != ? "
                "GROUP BY login, emote", (since, STREAMER_NAME.lower())).fetchall()
            channels = [c for (c,) in db.execute(
                "SELECT DISTINCT channel FROM emote_sightings WHERE ts >= ? AND login != ? ORDER BY channel",
                (since, STREAMER_NAME.lower()))]
        finally:
            db.close()
    except sqlite3.Error as e:
        print(f"[EMOTECARD] could not read {ANALYTICS_DB}: {e}")
        return [], [], 0, []

    per_emote: dict[str, int] = {}
    per_user: dict[str, dict[str, int]] = {}
    for login, emote, n in rows:
        per_emote[emote] = per_emote.get(emote, 0) + n
        per_user.setdefault(login, {})[emote] = n
    cols = sorted(per_emote, key=lambda e: (-per_emote[e], e))[:_MAX_COLS]
    users = sorted(((u, counts, sum(counts.values())) for u, counts in per_user.items()),
                   key=lambda r: -r[2])
    return cols, users, sum(per_emote.values()), channels


def _table(cols: list[str], users: list[tuple[str, dict[str, int], int]]) -> str:
    """Viewers down the side, emotes across the top. The shared `howlin67`
    prefix is dropped from the headers — it's on every one of them."""
    short = [c[len(EMOTE_PREFIX):] if c.lower().startswith(EMOTE_PREFIX.lower()) else c
             for c in cols]
    w_who = max(len("viewer"), *(len(u) for u, _, _ in users[:_MAX_ROWS]))
    w_col = [max(len(h), 3) for h in short]
    head = f"{'viewer':<{w_who}}  " + "  ".join(
        f"{h:>{w}}" for h, w in zip(short, w_col)) + "   all"
    lines = [head, "-" * len(head)]
    for who, counts, total in users[:_MAX_ROWS]:
        cells = "  ".join(f"{counts.get(c, 0) or '·':>{w}}" for c, w in zip(cols, w_col))
        lines.append(f"{who:<{w_who}}  {cells}   {total:>3}")
    if len(users) > _MAX_ROWS:
        lines.append(f"... and {len(users) - _MAX_ROWS} more")
    return "```\n" + "\n".join(lines) + "\n```"


def _embed() -> discord.Embed:
    cols, users, total, channels = _grid(EMOTE_CARD_DAYS)
    e = discord.Embed(
        title="Emotes used in other channels",
        colour=0x9146FF,
        description=(_table(cols, users) if users else
                     "Nobody has used one of our emotes in another channel in this window."))
    e.add_field(name="Window", value=f"Last {EMOTE_CARD_DAYS} days", inline=True)
    e.add_field(name="Uses", value=str(total), inline=True)
    e.add_field(name="People", value=str(len(users)), inline=True)
    if channels:
        e.add_field(name="Seen in", value=", ".join(f"#{c}" for c in channels[:12]), inline=False)
    e.set_footer(text=f"Read from ~200 live channels · updated {datetime.now():%-d %b %H:%M}")
    return e


async def render(bot) -> None:
    """Draw the card, editing the existing message in place; recreate it once
    if it was deleted."""
    async with _lock:
        try:
            channel = (bot.get_channel(EMOTE_CARD_CHANNEL_ID)
                       or await bot.fetch_channel(EMOTE_CARD_CHANNEL_ID))
            embed = _embed()
            mid = panel_message_get(_PANEL)
            if mid:
                try:
                    await channel.get_partial_message(mid).edit(embed=embed)
                    return
                except discord.NotFound:
                    pass  # deleted — recreate below
            msg = await channel.send(embed=embed)
            panel_message_set(_PANEL, msg.id)
        except Exception as e:
            log_error(f"[EMOTECARD] render failed: {e!r}")


async def _loop(bot) -> None:
    global _drawn_on
    await bot.wait_until_ready()
    await asyncio.sleep(15)
    fails = 0
    while not bot.is_closed():
        try:
            today = date.today()
            if _drawn_on != today:
                await render(bot)
                _drawn_on = today
            fails = 0
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[EMOTECARD] tick failed (attempt {fails}): {e!r}")
        await asyncio.sleep(_TICK_SECONDS)


def start(bot) -> None:
    if getattr(bot, "_emotecard_started", False) or not EMOTE_CARD_CHANNEL_ID:
        return
    bot._emotecard_started = True
    bot.loop.create_task(_loop(bot))
