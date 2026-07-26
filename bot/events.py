import discord

from .config import (
    GUILD_ID,
    SPOTIFY_VOICE_CHANNEL_ID,
    COMMAND_LOG_CHANNEL_ID,
)
from .spotify import count_humans_in_channel, handle_spotify_auto_pause
from .leetcode import leetcode_daily_scheduler, leetcode_contest_scheduler, leetcode_premium_weekly_scheduler
from .voicechat import on_voice_update
from .logbus import log_error, start as logbus_start
from .fairaccess import start as fairaccess_start, on_voice_state as fairaccess_voice
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

    if not getattr(bot, "_premium_weekly_task_started", False):
        bot._premium_weekly_task_started = True
        bot.loop.create_task(leetcode_premium_weekly_scheduler(bot))

    # fair-access cooldown system (tracked rooms + admin panel)
    fairaccess_start(bot)



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

    # --- Fair-access tracked-room tally/cooldowns + attendance sessions ---
    try:
        await fairaccess_voice(bot, member, before, after)
    except Exception as e:
        log_error(f"[VOICE] fair-access failed: {e!r}")

    # --- Broadcast to any active voice-chat overlay sessions ---
    try:
        if before_id:
            await on_voice_update(bot, before_id)
        if after_id and after_id != before_id:
            await on_voice_update(bot, after_id)
    except Exception as e:
        log_error(f"[VOICE] overlay broadcast failed: {e!r}")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if not COMMAND_LOG_CHANNEL_ID:
        return
    if interaction.type != discord.InteractionType.application_command:
        return

    # Skip logging for admins/mods
    if isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

    cmd_data = interaction.data or {}
    cmd_name = cmd_data.get("name", "unknown")

    options = cmd_data.get("options") or []
    options_str = "\n".join(f"`{o['name']}`: {o.get('value', '')}" for o in options) if options else None

    channel_mention = interaction.channel.mention if interaction.channel else "unknown"

    embed = discord.Embed(color=0x5865F2, timestamp=discord.utils.utcnow())
    embed.add_field(name="User", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=True)
    embed.add_field(name="Command", value=f"`/{cmd_name}`", inline=True)
    embed.add_field(name="Channel", value=channel_mention, inline=True)
    if options_str:
        embed.add_field(name="Options", value=options_str, inline=False)
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    try:
        log_channel = bot.get_channel(COMMAND_LOG_CHANNEL_ID) or await bot.fetch_channel(COMMAND_LOG_CHANNEL_ID)
        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"[COMMAND LOG] {e}")
