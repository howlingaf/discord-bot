import asyncio
import re

import discord
from discord import app_commands

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    GUILD_ID,
    LEETCODE_WEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_RECAP_CHANNEL_ID,
    SECRET_STREAMS_CHANNEL_ID,
    TWITCH_CONSOLE_CHANNEL_ID,
)
from .twitchconsole import call_console
from .spotify import dm_spotify_link
from .leetcode import (
    post_leetcode_contest,
    post_pre_contest,
    post_leetcode_problem,
    post_leetcode_weekly_premium,
    get_or_create_problem_post,
    _classify_contest,
)
from .database import (
    leetcode_delete_problem,
    leetcode_get_problem,
    leetcode_get_problem_by_slug,
    twitch_link_delete,
)
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


@bot.tree.command(name="delete", description="(Admin) Delete a problem post by LeetCode ID.")
@app_commands.describe(question_id="The LeetCode problem number to delete (e.g. 67)")
@app_commands.checks.has_permissions(manage_messages=True)
async def delete_problem(interaction: discord.Interaction, question_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = leetcode_delete_problem(str(question_id))
        if not deleted:
            await interaction.followup.send(f"\u274c Problem #{question_id} not found.", ephemeral=True)
            return

        # Delete the forum post
        try:
            thread = bot.get_channel(deleted["thread_id"]) or await bot.fetch_channel(deleted["thread_id"])
            await thread.delete()
        except Exception:
            pass

        await interaction.followup.send(f"\u2705 Deleted problem #{question_id} ({deleted['title']}).", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="daily", description="(Admin) Post today's LeetCode daily problem (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def daily(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_problem(bot, force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="weekly", description="(Admin) Post the pre-contest thread for the upcoming weekly contest.")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_pre_contest(bot, "weekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="biweekly", description="(Admin) Post the pre-contest thread for the upcoming biweekly contest.")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def biweekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_pre_contest(bot, "biweekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="premium-weekly", description="(Admin) Post this week's premium weekly problem (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def premium_weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_weekly_premium(bot, force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="test", description="(Admin) Dry-run a posting command \u2014 see what it would do without posting anything.")
@app_commands.describe(
    command="Which posting command to dry-run",
)
@app_commands.choices(command=[
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="biweekly", value="biweekly"),
    app_commands.Choice(name="premium-weekly", value="premium-weekly"),
])
@app_commands.checks.has_permissions(manage_messages=True)
async def test_cmd(interaction: discord.Interaction, command: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    name = command.value
    try:
        if name == "daily":
            _, msg = await post_leetcode_problem(bot, force=True, dry_run=True)
        elif name == "weekly":
            _, msg = await post_pre_contest(bot, "weekly", force=True, dry_run=True)
        elif name == "biweekly":
            _, msg = await post_pre_contest(bot, "biweekly", force=True, dry_run=True)
        elif name == "premium-weekly":
            _, msg = await post_leetcode_weekly_premium(bot, force=True, dry_run=True)
        else:
            await interaction.followup.send(f"\u274c Unknown command: {name}", ephemeral=True)
            return
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /test {name}] {e!r}")
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


@bot.tree.command(name="post-solution", description="(Admin) Post a solution submission to a problem's forum thread.")
@app_commands.describe(slug="Problem slug (e.g. clone-graph)", user="Username to credit", url="Submission URL")
@app_commands.checks.has_permissions(manage_messages=True)
async def post_solution(interaction: discord.Interaction, slug: str, user: str, url: str):
    await interaction.response.defer(ephemeral=True)
    try:
        existing = leetcode_get_problem_by_slug(slug)
        if not existing:
            await interaction.followup.send(f"\u274c No forum post found for '{slug}'", ephemeral=True)
            return

        from .twitchlink import solution_name, maybe_prompt
        thread = bot.get_channel(existing["thread_id"]) or await bot.fetch_channel(existing["thread_id"])
        await maybe_prompt(bot, user)
        content = f"{solution_name(user)} submitted a solution!\n<{url}>"
        await thread.send(content, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f"\u2705 Posted {user}'s solution to {slug}", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="update-recap", description="(Admin) Add a problem to the latest stream recap embed.")
@app_commands.describe(slug="Problem slug (e.g. clone-graph)")
@app_commands.checks.has_permissions(manage_messages=True)
async def update_recap(interaction: discord.Interaction, slug: str):
    await interaction.response.defer(ephemeral=True)
    try:
        from .recap import resolve_slug_to_question_id

        session = bot.http_session
        if not session:
            await interaction.followup.send("\u274c Bot HTTP session not ready", ephemeral=True)
            return

        question_id = await resolve_slug_to_question_id(session, slug)
        if not question_id:
            await interaction.followup.send(f"\u274c Could not resolve slug '{slug}'", ephemeral=True)
            return

        thread_id, err = await get_or_create_problem_post(bot, question_id)
        if not thread_id:
            await interaction.followup.send(f"\u274c Could not get/create post: {err}", ephemeral=True)
            return

        channel = bot.get_channel(LEETCODE_RECAP_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_RECAP_CHANNEL_ID)

        last_msg = None
        async for msg in channel.history(limit=10):
            if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "Stream Recap":
                last_msg = msg
                break

        if not last_msg:
            await interaction.followup.send("\u274c No recent recap message found", ephemeral=True)
            return

        embed = last_msg.embeds[0]
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
        problem_name = slug.replace("-", " ").title()
        new_line = f"[{question_id}. {problem_name}]({thread_url})"

        desc = embed.description or ""
        if new_line in desc:
            await interaction.followup.send(f"\u2139\ufe0f {question_id}. {problem_name} already in recap", ephemeral=True)
            return

        new_embed = discord.Embed(
            title=embed.title,
            description=(desc + "\n\n" + new_line).strip(),
            color=embed.color,
        )
        await last_msg.edit(embed=new_embed)
        await interaction.followup.send(f"\u2705 Added {question_id}. {problem_name} to recap", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="contest-recap", description="(Admin) Post a recap for any contest by slug.")
@app_commands.describe(slug="The contest slug (e.g. weekly-contest-488)")
@app_commands.checks.has_permissions(manage_messages=True)
async def contest_recap(interaction: discord.Interaction, slug: str):
    await interaction.response.defer(ephemeral=True)
    try:
        contest_type = _classify_contest(slug)
        if not contest_type:
            await interaction.followup.send("\u274c Slug must start with 'weekly-contest-' or 'biweekly-contest-'.", ephemeral=True)
            return

        title = slug.replace("-", " ").title()
        mock_contest = {"title": title, "titleSlug": slug, "startTime": 0, "duration": 5400}

        posted, msg = await post_leetcode_contest(
            bot, contest_type, force=True, contests=[mock_contest],
        )
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


# ---- Fix problem embed superscripts ----

def _fix_sup_text(text: str) -> str:
    if not text:
        return text
    # 10^4 through 10^9 (e.g. 104 → 10^4). Single-digit only — two-digit
    # would catch binary representations like 1010 in problem statements.
    text = re.sub(r'(?<!\d)10([4-9])(?!\d)', r'10^\1', text)
    # 2^31 (signed 32-bit int bound, e.g. -231 → -2^31)
    text = re.sub(r'(?<!\d)2(31)(?!\d)', r'2^\1', text)
    return text


def _strip_sup_text(text: str) -> str:
    """Reverse accidental caret insertions in example embeds: 10^N → 10N, 2^31 → 231."""
    if not text:
        return text
    text = re.sub(r'10\^(\d+)', r'10\1', text)
    text = re.sub(r'2\^(31)', r'231', text)
    return text


def _apply_embed_transform(embed: discord.Embed, fn) -> tuple[discord.Embed, bool]:
    d = embed.to_dict()
    changed = False
    if d.get('description'):
        fixed = fn(d['description'])
        if fixed != d['description']:
            d['description'] = fixed
            changed = True
    for field in d.get('fields', []):
        fixed = fn(field.get('value', ''))
        if fixed != field.get('value', ''):
            field['value'] = fixed
            changed = True
    return discord.Embed.from_dict(d), changed


def _fix_embed_superscripts(embed: discord.Embed) -> tuple[discord.Embed, bool]:
    return _apply_embed_transform(embed, _fix_sup_text)


def _strip_embed_superscripts(embed: discord.Embed) -> tuple[discord.Embed, bool]:
    return _apply_embed_transform(embed, _strip_sup_text)


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
async def on_message_edit(_before: discord.Message, after: discord.Message):
    await on_chat_edit(after)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await on_chat_delete(payload)
