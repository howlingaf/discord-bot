import discord

from .config import (
    GUILD_ID,
    SPOTIFY_VOICE_CHANNEL_ID,
)
from .spotify import count_humans_in_channel, handle_spotify_auto_pause
from .leetcode import leetcode_daily_scheduler, leetcode_contest_scheduler, leetcode_premium_weekly_scheduler
from .status import leetcode_status_scheduler
from .client import bot


@bot.event
async def on_ready():
    print(f"\u2705 Logged in as {bot.user} (id={bot.user.id})")

    # start LeetCode schedulers once
    if not getattr(bot, "_daily_task_started", False):
        bot._daily_task_started = True
        bot.loop.create_task(leetcode_daily_scheduler(bot))

    if not getattr(bot, "_contest_task_started", False):
        bot._contest_task_started = True
        bot.loop.create_task(leetcode_contest_scheduler(bot))

    if not getattr(bot, "_status_task_started", False):
        bot._status_task_started = True
        bot.loop.create_task(leetcode_status_scheduler(bot))

    if not getattr(bot, "_premium_weekly_task_started", False):
        bot._premium_weekly_task_started = True
        bot.loop.create_task(leetcode_premium_weekly_scheduler(bot))


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
