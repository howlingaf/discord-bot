import discord

from .config import (
    GUILD_ID,
    SPOTIFY_VOICE_CHANNEL_ID,
)
from .spotify import count_humans_in_channel, handle_spotify_auto_pause
from .leetcode import leetcode_daily_scheduler, leetcode_contest_scheduler
from .voicechat import on_voice_update
from .logbus import log_error, start as logbus_start
from .fairaccess import start as fairaccess_start, on_voice_state as fairaccess_voice
from .solvesweep import on_voice_state as solvesweep_voice
from .voicenames import on_voice_state as voicenames_voice, start as voicenames_start
from .client import bot


@bot.event
async def on_ready():
    print(f"\u2705 Logged in as {bot.user} (id={bot.user.id})")

    # start the #discord-log error forwarder before the schedulers
    logbus_start(bot)

    # register restart-safe Twitch-link approval components
    from .twitchlink import register as twitchlink_register
    twitchlink_register(bot)

    # start the relay that posts Twitch-bot logs into #twitch-bot-console
    from .twitchlog import start as twitchlog_start
    twitchlog_start(bot)

    # start LeetCode schedulers once
    if not getattr(bot, "_daily_task_started", False):
        bot._daily_task_started = True
        bot.loop.create_task(leetcode_daily_scheduler(bot))

    if not getattr(bot, "_contest_task_started", False):
        bot._contest_task_started = True
        bot.loop.create_task(leetcode_contest_scheduler(bot))


    # keep the #info card's "usually around <time>" correct across DST
    if not getattr(bot, "_infocard_task_started", False):
        bot._infocard_task_started = True
        from .infocard import scheduler as infocard_scheduler
        bot.loop.create_task(infocard_scheduler(bot))

    # fair-access cooldown system (tracked rooms + admin panel)
    fairaccess_start(bot)

    # restore the chill room's name if whoever renamed it left while we were down
    voicenames_start(bot)



@bot.event
async def on_member_join(member: discord.Member):
    from .welcome import on_member_join as welcome_join
    try:
        await welcome_join(bot, member)
    except Exception as e:
        log_error(f"[JOIN] welcome failed: {e!r}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None

    # Each subsystem is isolated: one of them raising must never cost the others
    # their event. (A dead Spotify token used to abort the whole handler, so
    # attendance sessions silently never closed.)

    # --- Spotify auto-pause ---
    try:
        if SPOTIFY_VOICE_CHANNEL_ID and (before_id == SPOTIFY_VOICE_CHANNEL_ID or after_id == SPOTIFY_VOICE_CHANNEL_ID):
            guild = bot.get_guild(GUILD_ID)
            if guild:
                channel = guild.get_channel(SPOTIFY_VOICE_CHANNEL_ID)
                if isinstance(channel, discord.VoiceChannel):
                    member_count = count_humans_in_channel(channel)
                    if bot.http_session:
                        await handle_spotify_auto_pause(bot.http_session, member_count)
    except Exception as e:
        log_error(f"[VOICE] spotify auto-pause failed: {e!r}")

    # --- Temporary channel name: revert once the setter leaves ---
    try:
        await voicenames_voice(bot, member, before, after)
    except Exception as e:
        log_error(f"[VOICE] name revert failed: {e!r}")

    # --- Fair-access tracked-room tally/cooldowns + attendance sessions ---
    try:
        await fairaccess_voice(bot, member, before, after)
    except Exception as e:
        log_error(f"[VOICE] fair-access failed: {e!r}")

    # --- Solve sweep: a finished co-working session posts its problems ---
    # After fair-access, which closes the visit row this reads.
    try:
        await solvesweep_voice(bot, member, before, after)
    except Exception as e:
        log_error(f"[VOICE] solve sweep failed: {e!r}")

    # --- Broadcast to any active voice-chat overlay sessions ---
    try:
        if before_id:
            await on_voice_update(bot, before_id)
        if after_id and after_id != before_id:
            await on_voice_update(bot, after_id)
    except Exception as e:
        log_error(f"[VOICE] overlay broadcast failed: {e!r}")
