import urllib.parse

import discord

from .config import (
    GUILD_ID,
    VERIFIED_ROLE_ID,
    VERIFY_FALLBACK_CHANNEL_ID,
    SPOTIFY_VOICE_CHANNEL_ID,
    PUBLIC_BASE_URL,
)
from .database import has_mapping, create_state
from .twitch import dm_verify_link
from .spotify import count_humans_in_channel, handle_spotify_auto_pause
from .leetcode import leetcode_daily_poller, leetcode_contest_poller
from .client import bot


@bot.event
async def on_ready():
    print(f"\u2705 Logged in as {bot.user} (id={bot.user.id})")

    # start LeetCode pollers once
    if not getattr(bot, "_daily_task_started", False):
        bot._daily_task_started = True
        bot.loop.create_task(leetcode_daily_poller(bot))

    if not getattr(bot, "_contest_task_started", False):
        bot._contest_task_started = True
        bot.loop.create_task(leetcode_contest_poller(bot))


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}
    added = after_roles - before_roles

    if VERIFIED_ROLE_ID not in added:
        return

    if has_mapping(after.id):
        return

    try:
        await dm_verify_link(after)
        return
    except discord.Forbidden:
        pass
    except Exception:
        pass

    if not VERIFY_FALLBACK_CHANNEL_ID:
        return

    channel = bot.get_channel(VERIFY_FALLBACK_CHANNEL_ID)
    if not channel:
        return

    try:
        state = create_state(after.id)
        url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
        await channel.send(f"{after.mention} verify your Twitch account here:\n{url}")
    except Exception:
        pass


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not SPOTIFY_VOICE_CHANNEL_ID:
        return

    watched_id = SPOTIFY_VOICE_CHANNEL_ID
    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None
    if before_id != watched_id and after_id != watched_id:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(watched_id)
    if not isinstance(channel, discord.VoiceChannel):
        return

    member_count = count_humans_in_channel(channel)
    if bot.http_session:
        await handle_spotify_auto_pause(bot.http_session, member_count)
