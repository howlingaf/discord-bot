import discord
from discord import app_commands

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    GUILD_ID,
    SECRET_STREAMS_CHANNEL_ID,
    TWITCH_CONSOLE_CHANNEL_ID,
)
from .interest import (
    on_reaction_add,
    on_reaction_remove,
    on_reaction_clear,
    on_reaction_clear_emoji,
)
from .twitchconsole import call_console
from .spotify import dm_spotify_link
from .leetcode import get_or_create_problem_post
from .database import twitch_link_delete
from .voicechat import on_chat_message, on_chat_edit, on_chat_delete, register_command as vc_register_command
from .logbus import log_error
from .client import bot

vc_register_command(bot)


@bot.tree.command(name="spotifylink", description="(Owner) DM yourself the Spotify link so the bot can auto pause/resume.")
async def spotifylink(interaction: discord.Interaction):
    if SPOTIFY_ALLOWED_USER_ID and interaction.user.id != SPOTIFY_ALLOWED_USER_ID:
        await interaction.response.send_message("\u274c Not allowed.", ephemeral=True)
        return

    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI):
        await interaction.response.send_message("\u274c Spotify env not configured.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this inside the server.", ephemeral=True)
        return

    try:
        await dm_spotify_link(member)
        await interaction.response.send_message("\u2705 Check your DMs for the Spotify link.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("\u274c I can't DM you.", ephemeral=True)


@bot.tree.command(name="problem", description="Look up or create a forum post for a LeetCode problem by ID.")
@app_commands.describe(question_id="The LeetCode problem number (e.g. 67)")
async def problem(interaction: discord.Interaction, question_id: int):
    if question_id < 1:
        await interaction.response.send_message("\u274c Invalid problem ID.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        thread_id, err = await get_or_create_problem_post(bot, str(question_id))
        if thread_id:
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            await interaction.followup.send(thread_url, ephemeral=True)
        else:
            await interaction.followup.send(f"\u274c {err}", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        # Surface the real failure instead of always blaming the problem ID \u2014
        # the genuine "not found" case is already handled by the err branch above.
        await interaction.followup.send(f"\u274c Failed: {e!r}", ephemeral=True)


@bot.tree.command(name="twitch-unlink", description="(Admin) Forget a Twitch\u2194Discord link so the handle can be re-prompted.")
@app_commands.describe(handle="The Twitch handle to forget")
@app_commands.checks.has_permissions(manage_messages=True)
async def twitch_unlink(interaction: discord.Interaction, handle: str):
    removed = twitch_link_delete(handle.strip().lower())
    if removed:
        await interaction.response.send_message(
            f"\u2705 Forgot Twitch link for **{handle.strip().lower()}** \u2014 it'll be prompted again on the next solution.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"\u2139\ufe0f No stored link for **{handle.strip().lower()}**.", ephemeral=True)


@bot.tree.command(name="twitch", description="(Admin) Run a console command on the Twitch bot.")
@app_commands.describe(command="Which Twitch-bot command to run", args="Message to send (required for 'say'; ignored by the others)")
@app_commands.choices(command=[
    app_commands.Choice(name="status", value="status"),
    app_commands.Choice(name="clear", value="lt_clear"),
    app_commands.Choice(name="test", value="test"),
    app_commands.Choice(name="say", value="say"),
])
@app_commands.checks.has_permissions(manage_messages=True)
async def twitch_console(interaction: discord.Interaction, command: app_commands.Choice[str], args: str | None = None):
    # Accept only in the configured twitch-bot-console channel, from mods/owner.
    if not TWITCH_CONSOLE_CHANNEL_ID:
        await interaction.response.send_message(
            "\u274c Twitch console channel isn't configured (set TWITCH_CONSOLE_CHANNEL_ID).", ephemeral=True)
        return
    if interaction.channel_id != TWITCH_CONSOLE_CHANNEL_ID:
        await interaction.response.send_message(
            f"\u274c Use this in <#{TWITCH_CONSOLE_CHANNEL_ID}>.", ephemeral=True)
        return

    # 'say' posts to Twitch chat, so it needs a message \u2014 don't call the API without one.
    if command.value == "say" and not (args and args.strip()):
        await interaction.response.send_message(
            "\u274c `say` needs a message \u2014 e.g. `/twitch command:say args:hello chat \ud83d\udc4b`.", ephemeral=True)
        return

    await interaction.response.defer()
    ok, output = await call_console(bot.http_session, command.value, args or "")
    text = f"{'\u2705' if ok else '\u274c'} {output}"
    if len(text) > 1900:
        text = text[:1900] + "\u2026"
    await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())


# ---- Secret streams rename ----

@bot.tree.command(name="rename", description="(Admin) Rename the secret streams voice channel.")
@app_commands.describe(name="New name for the channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def rename_stream(interaction: discord.Interaction, name: str):
    if not SECRET_STREAMS_CHANNEL_ID:
        await interaction.response.send_message("Channel not configured.", ephemeral=True)
        return

    channel = bot.get_channel(SECRET_STREAMS_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("Channel not found.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        await channel.edit(name=name)
        await interaction.followup.send(f"Renamed to **{name}**.", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"Failed: {e}", ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    await on_chat_message(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await on_reaction_add(bot, payload)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await on_reaction_remove(bot, payload)


@bot.event
async def on_raw_reaction_clear(payload: discord.RawReactionClearEvent):
    await on_reaction_clear(bot, payload)


@bot.event
async def on_raw_reaction_clear_emoji(payload: discord.RawReactionClearEmojiEvent):
    await on_reaction_clear_emoji(bot, payload)


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    await on_chat_edit(after)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    from .schedule import on_message_delete as schedule_on_message_delete
    schedule_on_message_delete(payload)
    await on_chat_delete(payload)
