import discord
from discord import app_commands

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    GUILD_ID,
    TWITCH_CONSOLE_CHANNEL_ID,
    VOICE_NAME_CHANNEL_ID,
)
from .twitchconsole import call_console
from .spotify import dm_spotify_link
from .leetcode import get_or_create_problem_post
from .codeforces import get_or_create_problem_post as cf_get_or_create_post
from .database import twitch_link_delete
from .voicechat import on_chat_message, on_chat_edit, on_chat_delete, register_command as vc_register_command
from .voicenames import rename as vc_rename
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


@bot.tree.command(name="lc", description="Look up or create a forum post for a LeetCode problem by ID.")
@app_commands.describe(question_id="The LeetCode problem number (e.g. 67)")
async def lc(interaction: discord.Interaction, question_id: int):
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


@bot.tree.command(name="cf", description="Look up or create a forum post for a Codeforces problem.")
@app_commands.describe(problem="Problem link, or a bare reference like 1421A")
async def cf(interaction: discord.Interaction, problem: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Takes the raw string: the same helper the recap uses already accepts
        # either URL shape or a bare ref, and reports an unusable one as err.
        thread_id, err = await cf_get_or_create_post(bot, problem)
        if thread_id:
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            await interaction.followup.send(thread_url, ephemeral=True)
        else:
            await interaction.followup.send(f"\u274c {err}", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {e!r}", ephemeral=True)


@bot.tree.command(name="name", description="Temporarily rename the chill voice channel while you're in it.")
@app_commands.describe(name="What to call it until you leave")
@app_commands.checks.has_permissions(manage_messages=True)
async def name_channel(interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]):
    channel = bot.get_channel(VOICE_NAME_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("That channel isn't set up.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not any(m.id == member.id for m in channel.members):
        await interaction.response.send_message(
            f"Join {channel.mention} first \u2014 the name lasts while you're in there.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        msg = await vc_rename(channel, name, member.id)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        msg = f"Failed: {e}"
    await interaction.followup.send(msg, ephemeral=True)


# Kept only to point people at the new name \u2014 Discord still offers /problem in
# the picker for anyone who learned it, and a silent "unknown command" would
# read as the bot being broken.
@bot.tree.command(name="problem", description="Renamed \u2014 use /lc instead.")
@app_commands.describe(question_id="The LeetCode problem number (e.g. 67)")
async def problem(interaction: discord.Interaction, question_id: int | None = None):
    hint = f"/lc {question_id}" if question_id else "/lc {id}"
    await interaction.response.send_message(
        f"That's `{hint}` now.", ephemeral=True)


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


# ---- Fair-access cooldown system ----

from . import fairaccess as _fa

fa_whitelist = app_commands.Group(
    name="whitelist", description="(Admin) Fair-access whitelist",
    default_permissions=discord.Permissions(manage_messages=True))
fa_cooldown = app_commands.Group(
    name="cooldown", description="(Admin) Fair-access cooldowns",
    default_permissions=discord.Permissions(manage_messages=True))


@fa_whitelist.command(name="add", description="(Admin) Exempt a user from fair-access cooldowns.")
@app_commands.describe(user="Who to whitelist")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_wl_add(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.whitelist_add(bot, user.id, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_whitelist.command(name="remove", description="(Admin) Remove a user from the whitelist (their tally resets).")
@app_commands.describe(user="Who to remove")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_wl_remove(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.whitelist_remove(bot, user.id, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_whitelist.command(name="seed", description="(Admin) One-time import of the Verified role's current members.")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_wl_seed(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.whitelist_seed(bot, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_cooldown.command(name="release", description="(Admin) End a user's cooldown early (silent).")
@app_commands.describe(user="Whose cooldown to release")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_cd_release(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.cooldown_release(bot, user.id, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_cooldown.command(name="reset", description="(Admin) Zero a user's current session tally.")
@app_commands.describe(user="Whose tally to reset")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_cd_reset(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.session_reset(bot, user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_cooldown.command(name="apply", description="(Admin) Manually apply a cooldown.")
@app_commands.describe(user="Who to cool down", days="Duration in days (default 7)")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_cd_apply(interaction: discord.Interaction, user: discord.User,
                      days: app_commands.Range[int, 1, 90] | None = None):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.cooldown_apply(bot, user.id, interaction.user.id, days)
    await interaction.followup.send(msg, ephemeral=True)


@fa_cooldown.command(name="resetall",
                     description="(Admin) Restart the clock on every active cooldown.")
@app_commands.describe(days="New length in days, counted from now (default 7)")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_cd_resetall(interaction: discord.Interaction,
                         days: app_commands.Range[int, 1, 90] | None = None):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.cooldown_reset_all(bot, days)
    await interaction.followup.send(msg, ephemeral=True)


@fa_cooldown.command(name="indefinite",
                     description="(Admin) Drop the expiry from every active cooldown.")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_cd_indefinite(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.cooldown_make_indefinite(bot)
    await interaction.followup.send(msg, ephemeral=True)


bot.tree.add_command(fa_whitelist)
bot.tree.add_command(fa_cooldown)


@bot.event
async def on_message(message: discord.Message):
    await on_chat_message(message)


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    await on_chat_edit(after)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await on_chat_delete(payload)
