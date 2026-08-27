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
from .leetcode import get_or_create_problem_post_from_ref as lc_get_or_create_post
from .codeforces import get_or_create_problem_post as cf_get_or_create_post
from .problemsites import get_or_create_problem_post as site_get_or_create_post
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


async def _reply_with_post(interaction: discord.Interaction, lookup) -> None:
    """Shared body of the problem commands: await a (thread_id, err) lookup and
    answer with the thread link, ephemerally."""
    await interaction.response.defer(ephemeral=True)
    try:
        thread_id, err = await lookup
        if thread_id:
            await interaction.followup.send(
                f"https://discord.com/channels/{GUILD_ID}/{thread_id}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"❌ Failed: {e!r}", ephemeral=True)


@bot.tree.command(name="lc", description="Look up or create a forum post for a LeetCode problem.")
@app_commands.describe(problem="Problem link, or the number (e.g. 67)")
async def lc(interaction: discord.Interaction, problem: str):
    await _reply_with_post(interaction, lc_get_or_create_post(bot, problem))


@bot.tree.command(name="cf", description="Look up or create a forum post for a Codeforces problem.")
@app_commands.describe(problem="Problem link, or a bare reference like 1421A")
async def cf(interaction: discord.Interaction, problem: str):
    await _reply_with_post(interaction, cf_get_or_create_post(bot, problem))


@bot.tree.command(name="cs", description="Look up or create a forum post for a CSES problem.")
@app_commands.describe(problem="Problem link, or the task number (e.g. 1068)")
async def cs(interaction: discord.Interaction, problem: str):
    await _reply_with_post(interaction, site_get_or_create_post(bot, "cses", problem))


@bot.tree.command(name="eu", description="Look up or create a forum post for a Project Euler problem.")
@app_commands.describe(problem="Problem link, or the problem number (e.g. 1)")
async def eu(interaction: discord.Interaction, problem: str):
    await _reply_with_post(interaction, site_get_or_create_post(bot, "euler", problem))


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
@app_commands.describe(problem="Problem link, or the number (e.g. 67)")
async def problem_stub(interaction: discord.Interaction, problem: str | None = None):
    hint = f"/lc {problem}" if problem else "/lc <link>"
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
@app_commands.describe(
    command="Which Twitch-bot command to run",
    args="say: the message. viewers: [regulars|viewers|emotes|streams|user NAME] [--since YYYY-MM-DD] [--top N]")
@app_commands.choices(command=[
    app_commands.Choice(name="status", value="status"),
    app_commands.Choice(name="clear", value="lt_clear"),
    app_commands.Choice(name="test", value="test"),
    app_commands.Choice(name="say", value="say"),
    app_commands.Choice(name="viewers", value="viewers"),
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
    for chunk in _chunk_message(text):
        await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())


def _chunk_message(text: str, limit: int = 1900) -> list[str]:
    """Split on line breaks to fit Discord's message cap. A code block that
    spans chunks is closed and reopened so every piece renders monospace —
    the viewers reports are tables and only make sense that way."""
    if len(text) <= limit:
        return [text]
    fenced = text.rstrip().endswith("```")
    body = text.rstrip()
    if fenced:
        body = body[:-3].rstrip()
    chunks, cur = [], ""
    for line in body.split("\n"):
        if len(cur) + len(line) + 1 > limit - 8:      # room for a closing fence
            chunks.append(cur)
            cur = "```\n" if fenced else ""
        cur += line + "\n"
    chunks.append(cur)
    if fenced:
        chunks = [c.rstrip() + "\n```" for c in chunks]
    return chunks


# ---- Fair-access cooldown system ----

from . import fairaccess as _fa

fa_regular = app_commands.Group(
    name="regular", description="(Admin) Regulars",
    default_permissions=discord.Permissions(manage_messages=True))


@fa_regular.command(name="add", description="(Admin) Mark a user a regular (hides the newcomer room).")
@app_commands.describe(user="Who to mark")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_reg_add(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.regular_add(bot, user.id, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_regular.command(name="remove", description="(Admin) Un-mark a regular, restoring the room (permanent).")
@app_commands.describe(user="Who to un-mark")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_reg_remove(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.regular_remove(bot, user.id, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


@fa_regular.command(name="removeall", description="(Admin) Un-mark every regular at once.")
@app_commands.checks.has_permissions(manage_messages=True)
async def fa_reg_removeall(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, msg = await _fa.regular_remove_all(bot, interaction.user.id)
    await interaction.followup.send(msg, ephemeral=True)


bot.tree.add_command(fa_regular)


@bot.event
async def on_message(message: discord.Message):
    await on_chat_message(message)


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    await on_chat_edit(after)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await on_chat_delete(payload)
