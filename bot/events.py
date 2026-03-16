import discord

from .config import (
    GUILD_ID,
    SPOTIFY_VOICE_CHANNEL_ID,
    COMMAND_LOG_CHANNEL_ID,
    SECRET_STREAMS_CHANNEL_ID,
    SECRET_STREAMS_EMPTY_NAME,
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

    # --- Secret streams channel rename ---
    await _check_secret_streams_rename(member, before, after)


async def _check_secret_streams_rename(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if not SECRET_STREAMS_CHANNEL_ID:
        return

    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None
    if before_id != SECRET_STREAMS_CHANNEL_ID and after_id != SECRET_STREAMS_CHANNEL_ID:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(SECRET_STREAMS_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return

    humans = count_humans_in_channel(channel)
    if humans == 0 and channel.name != SECRET_STREAMS_EMPTY_NAME:
        try:
            await channel.edit(name=SECRET_STREAMS_EMPTY_NAME)
        except Exception as e:
            print(f"[SECRET STREAMS] rename failed: {e}")


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
