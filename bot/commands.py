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
    post_contest_rankings,
    post_leetcode_problem,
    post_leetcode_weekly_premium,
    get_or_create_problem_post,
    get_or_create_problem_post_archived,
    get_zerotrac_data,
    build_contest_forum_embed,
    CONTEST_FORUM_CHANNEL_MAP,
    _classify_contest,
    _contest_difficulty_tag,
    _get_or_create_forum_tag,
)
from .database import (
    leetcode_delete_problem,
    leetcode_get_problem,
    leetcode_get_problem_by_slug,
    leetcode_contest_post_get,
    leetcode_contest_post_save,
    linked_users_get,
    linked_users_get_by_username,
    linked_users_set,
    linked_users_delete,
    twitch_link_delete,
    virtual_stats_get,
    virtual_stats_set,
    virtual_stats_update_rating,
    virtual_contest_history_get,
    virtual_contest_history_log,
    virtual_contest_history_complete,
    virtual_contest_history_done_slugs,
    virtual_contest_history_recent,
    virtual_problem_history_log,
    virtual_problem_history_done_slugs,
    virtual_problem_history_recent,
    virtual_reset,
    virtual_stats_set_last_contest,
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


@bot.tree.command(name="weekly-rankings", description="(Admin) Post rankings for a weekly contest (manual trigger).")
@app_commands.describe(number="Contest number (e.g. 490). Defaults to last posted contest.", force="If true, post even if already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def weekly_rankings(interaction: discord.Interaction, number: int | None = None, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        slug_override = f"weekly-contest-{number}" if number else None
        posted, msg = await post_contest_rankings(bot, "weekly", force=force, slug_override=slug_override)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="biweekly-rankings", description="(Admin) Post rankings for a biweekly contest (manual trigger).")
@app_commands.describe(number="Contest number (e.g. 177). Defaults to last posted contest.", force="If true, post even if already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def biweekly_rankings(interaction: discord.Interaction, number: int | None = None, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        slug_override = f"biweekly-contest-{number}" if number else None
        posted, msg = await post_contest_rankings(bot, "biweekly", force=force, slug_override=slug_override)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="link", description="Link your Discord account to your LeetCode profile.")
@app_commands.describe(username="Your LeetCode username")
async def link(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Verify the username exists on LeetCode and resolve its canonical form.
        # LeetCode usernames are case-insensitive, so we key off the canonical
        # spelling the API returns rather than the raw (possibly mis-cased) input
        # \u2014 otherwise "JohnDoe" and "johndoe" would link as two separate accounts.
        async with bot.http_session.get(f"https://leetcode-api-pied.vercel.app/user/{username}") as resp:
            if resp.status != 200:
                await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
                return
            data = await resp.json()

        canonical = data.get("username")
        if not canonical:
            await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
            return

        # Check if this LeetCode account is already claimed by someone else
        existing_owner = linked_users_get_by_username(canonical)
        if existing_owner and existing_owner != interaction.user.id:
            await interaction.followup.send("\u274c That LeetCode username is already linked to another user.", ephemeral=True)
            return

        linked_users_set(interaction.user.id, canonical)
        await interaction.followup.send(f"\u2705 Linked to **{canonical}**.", ephemeral=True)
    except Exception as e:
        log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="unlink", description="Unlink your LeetCode profile from your Discord account.")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    removed = linked_users_delete(interaction.user.id)
    if removed:
        await interaction.followup.send("\u2705 Your LeetCode account has been unlinked.", ephemeral=True)
    else:
        await interaction.followup.send("\u2139\ufe0f You don't have a linked LeetCode account.", ephemeral=True)


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
    number="Contest number for the rankings tests (defaults to last posted).",
)
@app_commands.choices(command=[
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="biweekly", value="biweekly"),
    app_commands.Choice(name="weekly-rankings", value="weekly-rankings"),
    app_commands.Choice(name="biweekly-rankings", value="biweekly-rankings"),
    app_commands.Choice(name="premium-weekly", value="premium-weekly"),
])
@app_commands.checks.has_permissions(manage_messages=True)
async def test_cmd(interaction: discord.Interaction, command: app_commands.Choice[str], number: int | None = None):
    await interaction.response.defer(ephemeral=True)
    name = command.value
    try:
        if name == "daily":
            _, msg = await post_leetcode_problem(bot, force=True, dry_run=True)
        elif name == "weekly":
            _, msg = await post_pre_contest(bot, "weekly", force=True, dry_run=True)
        elif name == "biweekly":
            _, msg = await post_pre_contest(bot, "biweekly", force=True, dry_run=True)
        elif name == "weekly-rankings":
            slug_override = f"weekly-contest-{number}" if number else None
            _, msg = await post_contest_rankings(bot, "weekly", force=True, dry_run=True, slug_override=slug_override)
        elif name == "biweekly-rankings":
            slug_override = f"biweekly-contest-{number}" if number else None
            _, msg = await post_contest_rankings(bot, "biweekly", force=True, dry_run=True, slug_override=slug_override)
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
@app_commands.describe(command="Which Twitch-bot command to run", args="Optional arguments")
@app_commands.choices(command=[
    app_commands.Choice(name="status", value="status"),
    app_commands.Choice(name="clear", value="lt_clear"),
    app_commands.Choice(name="test", value="test"),
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


# ---- Virtual rating system ----

_DEFAULT_RATING = 1500.0
_CONTEST_RADIUS = 100   # serve contests within this many points of current rating (symmetric)
_PRACTICE_RADIUS = 150  # serve problems within this many points of current rating (symmetric)


async def _init_virtual_stats(discord_user_id: int, lc_username: str, session) -> dict:
    """Fetch LeetCode contest stats and initialize user_virtual_stats. Returns stats dict."""
    try:
        async with session.get(f"https://leetcode-api-pied.vercel.app/user/{lc_username}/contests") as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                ranking = data.get("userContestRanking") or {}
                rating = float(ranking.get("rating") or _DEFAULT_RATING)
                live_count = int(ranking.get("attendedContestsCount") or 0)
            else:
                rating = _DEFAULT_RATING
                live_count = 0
    except Exception:
        rating = _DEFAULT_RATING
        live_count = 0

    virtual_stats_set(discord_user_id, rating=rating, live_contest_count=live_count, virtual_contest_count=0)
    return {"rating": rating, "live_contest_count": live_count, "virtual_contest_count": 0}


@bot.tree.command(name="rating", description="Show your current virtual rating and contest stats.")
async def rating_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    lc_username = linked_users_get(interaction.user.id)
    if not lc_username:
        await interaction.followup.send("❌ You need to link your LeetCode account first. Use `/link`.", ephemeral=True)
        return

    stats = virtual_stats_get(interaction.user.id)
    if not stats:
        stats = await _init_virtual_stats(interaction.user.id, lc_username, bot.http_session)

    embed = discord.Embed(title=f"📊 {lc_username}", color=0x9B59B6)
    embed.add_field(name="Server Rating", value=f"`{stats['rating']:.0f}`", inline=True)
    embed.add_field(name="Live Contests", value=f"`{stats['live_contest_count']}`", inline=True)
    embed.add_field(name="Virtual Contests", value=f"`{stats['virtual_contest_count']}`", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="get-contest", description="Get a virtual contest to practice at your current rating.")
async def get_contest_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    lc_username = linked_users_get(interaction.user.id)
    if not lc_username:
        await interaction.followup.send("❌ You need to link your LeetCode account first. Use `/link`.", ephemeral=True)
        return

    stats = virtual_stats_get(interaction.user.id)
    if not stats:
        stats = await _init_virtual_stats(interaction.user.id, lc_username, bot.http_session)

    zerotrac_entries = await get_zerotrac_data(bot.http_session)
    if not zerotrac_entries:
        await interaction.followup.send("❌ Could not load contest data.", ephemeral=True)
        return

    from collections import defaultdict
    contests_map: dict[str, list[dict]] = defaultdict(list)
    for e in zerotrac_entries:
        contests_map[e["contest_slug"]].append(e)

    done_contest_slugs = virtual_contest_history_done_slugs(interaction.user.id)
    # Also exclude contests that contain any problem the user has already practiced
    done_problem_slugs = virtual_problem_history_done_slugs(interaction.user.id)
    practiced_contest_slugs = {e["contest_slug"] for e in zerotrac_entries if e["title_slug"] in done_problem_slugs}
    done_slugs = done_contest_slugs | practiced_contest_slugs

    user_rating = stats["rating"]
    ratings_by_slug = {e["title_slug"]: e["rating"] for e in zerotrac_entries}

    # Build list of (slug, weighted_avg) for untaken contests
    candidates: list[tuple[str, float]] = []
    weights = {"Q1": 3, "Q2": 4, "Q3": 5, "Q4": 6}
    for slug, problems in contests_map.items():
        if slug in done_slugs:
            continue
        if _classify_contest(slug) is None:
            continue
        if len(problems) < 4:
            continue
        by_idx = {p["problem_index"]: p["rating"] for p in problems}
        total = total_w = 0
        for idx, w in weights.items():
            if idx not in by_idx:
                break
            total += by_idx[idx] * w
            total_w += w
        else:
            candidates.append((slug, total / total_w))

    if not candidates:
        await interaction.followup.send("❌ No more contests available.", ephemeral=True)
        return

    # Prefer contests within [rating-100, rating+100]; fall back to closest overall
    band = [(s, a) for s, a in candidates if abs(a - user_rating) <= _CONTEST_RADIUS]
    if band:
        best_slug, best_avg = min(band, key=lambda x: abs(x[1] - user_rating))
    else:
        best_slug, best_avg = min(candidates, key=lambda x: abs(x[1] - user_rating))

    # Get or create the contest forum thread
    post = leetcode_contest_post_get(best_slug)
    if post:
        contest_thread_id = post['thread_id']
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{contest_thread_id}"
    else:
        contest_type_str = _classify_contest(best_slug)
        forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type_str, 0)
        try:
            forum_channel = bot.get_channel(forum_channel_id) or await bot.fetch_channel(forum_channel_id)
            problems = sorted(contests_map[best_slug], key=lambda p: p["problem_index"])
            questions = [{"questionId": "", "title": p["title_slug"].replace("-", " ").title(), "titleSlug": p["title_slug"]} for p in problems]

            question_thread_ids: dict[str, int] = {}
            for p in problems:
                thread_id, _ = await get_or_create_problem_post_archived(bot, p["title_slug"])
                if thread_id:
                    question_thread_ids[p["title_slug"]] = thread_id

            contest_id_en = best_slug.replace("-", " ").title()
            mock_contest = {"title": contest_id_en, "titleSlug": best_slug, "startTime": 0}
            tag_name = _contest_difficulty_tag(questions, ratings_by_slug)
            contest_tag = await _get_or_create_forum_tag(forum_channel, tag_name)
            forum_embed = build_contest_forum_embed(mock_contest, questions, ratings_by_slug, question_thread_ids)
            result = await forum_channel.create_thread(
                name=contest_id_en[:100],
                embed=forum_embed,
                applied_tags=[contest_tag],
            )
            thread = result.thread if hasattr(result, "thread") else result
            leetcode_contest_post_save(best_slug, contest_type_str, thread.id, rated=1 if tag_name != "Rating Pending" else 0)
            contest_thread_id = thread.id
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{contest_thread_id}"
        except Exception as e:
            log_error(f"[CMD /{interaction.command.name if interaction.command else '?'}] {e!r}")
            await interaction.followup.send(f"❌ Could not create contest post: {e}", ephemeral=True)
            return

    # Unarchive the contest thread so the link resolves correctly in Discord
    try:
        contest_thread = bot.get_channel(contest_thread_id) or await bot.fetch_channel(contest_thread_id)
        if isinstance(contest_thread, discord.Thread) and contest_thread.archived:
            await contest_thread.edit(archived=False)
    except Exception as e:
        log_error(f"[GET-CONTEST] could not unarchive contest thread {contest_thread_id}: {e}")

    virtual_contest_history_log(interaction.user.id, best_slug, user_rating)
    virtual_stats_set_last_contest(interaction.user.id, best_slug)

    contest_label = best_slug.replace("-", " ").title()
    embed = discord.Embed(
        title=f"🏆 {contest_label}",
        description=f"Avg rating: ||{best_avg:.0f}|| | Your server rating: `{user_rating:.0f}`\n\n👉 {thread_url}",
        color=0x9B59B6,
    )
    embed.set_footer(text="Log your result with /set-contest — e.g. 1100 = solved Q1+Q2, missed Q3+Q4")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="set-contest", description="Log which problems you solved in your last virtual contest.")
@app_commands.describe(solved="4-digit binary string: 1=solved, 0=missed, left to right Q1→Q4 (e.g. 1100)")
async def set_contest_cmd(interaction: discord.Interaction, solved: str):
    await interaction.response.defer(ephemeral=True)

    if len(solved) != 4 or not all(c in "01" for c in solved):
        await interaction.followup.send("❌ `solved` must be 4 digits of 0s and 1s, e.g. `1100` for Q1+Q2 solved.", ephemeral=True)
        return

    lc_username = linked_users_get(interaction.user.id)
    if not lc_username:
        await interaction.followup.send("❌ You need to link your LeetCode account first. Use `/link`.", ephemeral=True)
        return

    stats = virtual_stats_get(interaction.user.id)
    if not stats or not stats.get("last_contest_slug"):
        await interaction.followup.send("❌ No contest to log. Run `/get-contest` first.", ephemeral=True)
        return

    slug = stats["last_contest_slug"]
    entry = virtual_contest_history_get(interaction.user.id, slug)

    if entry is None:
        await interaction.followup.send("❌ No contest to log. Run `/get-contest` first.", ephemeral=True)
        return

    if entry["rating_after"] is not None:
        await interaction.followup.send(
            f"ℹ️ You've already logged a result for `{slug.replace('-', ' ').title()}`. Your rating won't be updated.",
            ephemeral=True,
        )
        return

    import time as _time
    if _time.time() - entry["served_at"] > 86400:
        await interaction.followup.send(
            f"⏰ The 24-hour window for `{slug.replace('-', ' ').title()}` has expired. "
            f"Your rating won't be updated, but this contest is still marked as done.",
            ephemeral=True,
        )
        return

    # Get problem ratings for this contest from zerotrac
    zerotrac_entries = await get_zerotrac_data(bot.http_session)
    contest_problems = sorted(
        [e for e in zerotrac_entries if e["contest_slug"] == slug],
        key=lambda e: e["problem_index"]
    )[:4]

    if len(contest_problems) < 4:
        await interaction.followup.send(
            f"⚠️ Could not find ratings for `{slug.replace('-', ' ').title()}` — contest may be unrated. Rating not updated.",
            ephemeral=True,
        )
        return

    # Per-problem Elo: treat each problem as an opponent with its zerotrac rating
    K = 20.0
    old_rating = stats["rating"]
    delta = 0.0
    for flag, problem in zip([int(c) for c in solved], contest_problems):
        expected = 1.0 / (1.0 + 10 ** ((problem["rating"] - old_rating) / 400.0))
        delta += K * (flag - expected)

    new_rating = round(old_rating + delta, 1)
    virtual_contest_history_complete(interaction.user.id, slug, new_rating)
    virtual_stats_update_rating(interaction.user.id, new_rating)

    solved_display = " ".join(f"Q{i+1}{'✅' if c == '1' else '❌'}" for i, c in enumerate(solved))
    sign = "+" if delta >= 0 else ""
    await interaction.followup.send(
        f"✅ `{slug.replace('-', ' ').title()}`\n{solved_display}\nServer rating: `{old_rating:.0f}` → `{new_rating:.0f}` ({sign}{delta:.0f})",
        ephemeral=True,
    )


@bot.tree.command(name="practice", description="Get a problem to practice at your current rating level.")
async def practice_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    lc_username = linked_users_get(interaction.user.id)
    if not lc_username:
        await interaction.followup.send("❌ You need to link your LeetCode account first. Use `/link`.", ephemeral=True)
        return

    stats = virtual_stats_get(interaction.user.id)
    if not stats:
        stats = await _init_virtual_stats(interaction.user.id, lc_username, bot.http_session)

    zerotrac_entries = await get_zerotrac_data(bot.http_session)
    if not zerotrac_entries:
        await interaction.followup.send("❌ Could not load problem data.", ephemeral=True)
        return

    done_problem_slugs = virtual_problem_history_done_slugs(interaction.user.id)
    # Also exclude problems that appeared in any contest the user has already done
    done_contest_slugs = virtual_contest_history_done_slugs(interaction.user.id)
    contest_problem_slugs = {e["title_slug"] for e in zerotrac_entries if e["contest_slug"] in done_contest_slugs}
    excluded_slugs = done_problem_slugs | contest_problem_slugs

    user_rating = stats["rating"]

    candidates = [(e["title_slug"], e["rating"]) for e in zerotrac_entries if e["title_slug"] not in excluded_slugs]
    if not candidates:
        await interaction.followup.send("❌ You've done every rated problem. Impressive.", ephemeral=True)
        return

    # Prefer problems within [rating-150, rating+150]; fall back to closest overall
    band = [(s, r) for s, r in candidates if abs(r - user_rating) <= _PRACTICE_RADIUS]
    if band:
        best_slug, best_rating = min(band, key=lambda x: abs(x[1] - user_rating))
    else:
        best_slug, best_rating = min(candidates, key=lambda x: abs(x[1] - user_rating))

    thread_id = None
    existing = leetcode_get_problem_by_slug(best_slug)
    if existing:
        thread_id = existing["thread_id"]
    else:
        thread_id, _ = await get_or_create_problem_post_archived(bot, best_slug)

    if thread_id:
        try:
            problem_thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(problem_thread, discord.Thread) and problem_thread.archived:
                await problem_thread.edit(archived=False)
        except Exception as e:
            log_error(f"[PRACTICE] could not unarchive problem thread {thread_id}: {e}")

    virtual_problem_history_log(interaction.user.id, best_slug)

    title = best_slug.replace("-", " ").title()
    if thread_id:
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
        desc = f"Rating: ||{best_rating:.0f}|| | Your server rating: `{user_rating:.0f}`\n\n👉 {thread_url}"
    else:
        desc = f"Rating: ||{best_rating:.0f}|| | Your server rating: `{user_rating:.0f}`\n\n👉 https://leetcode.com/problems/{best_slug}/"

    embed = discord.Embed(title=f"📝 {title}", description=desc, color=0x9B59B6)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="history", description="Show your last 10 virtual contests and practice problems.")
async def history_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    lc_username = linked_users_get(interaction.user.id)
    if not lc_username:
        await interaction.followup.send("❌ You need to link your LeetCode account first. Use `/link`.", ephemeral=True)
        return

    contests = virtual_contest_history_recent(interaction.user.id, limit=10)
    problems = virtual_problem_history_recent(interaction.user.id, limit=10)

    embed = discord.Embed(title=f"📋 History — {lc_username}", color=0x9B59B6)

    if contests:
        lines = []
        for c in contests:
            label = c["contest_slug"].replace("-", " ").title()
            if c["rating_after"] is not None:
                delta = c["rating_after"] - c["rating_before"]
                sign = "+" if delta >= 0 else ""
                lines.append(f"**{label}** — `{c['rating_before']:.0f}` → `{c['rating_after']:.0f}` ({sign}{delta:.0f})")
            else:
                lines.append(f"**{label}** — `{c['rating_before']:.0f}` *(not logged)*")
        embed.add_field(name="🏆 Contests", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🏆 Contests", value="None yet.", inline=False)

    if problems:
        lines = [p["title_slug"].replace("-", " ").title() for p in problems]
        embed.add_field(name="📝 Problems", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="📝 Problems", value="None yet.", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="reset-rating", description="(Admin) Reset a user's virtual rating to their current LeetCode baseline.")
@app_commands.describe(user="The Discord user to reset")
@app_commands.checks.has_permissions(manage_messages=True)
async def reset_rating(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    lc_username = linked_users_get(user.id)
    if not lc_username:
        await interaction.followup.send("❌ That user doesn't have a linked LeetCode account.", ephemeral=True)
        return

    virtual_reset(user.id)
    stats = await _init_virtual_stats(user.id, lc_username, bot.http_session)
    await interaction.followup.send(
        f"✅ Reset **{user.display_name}**: rating set to `{stats['rating']:.0f}`, history wiped.",
        ephemeral=True,
    )


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


# ---- Commands-only channel ----

_COMMANDS_CHANNEL_ID = 1474376865400619189


@bot.event
async def on_message(message: discord.Message):
    await on_chat_message(message)
    if message.author.bot:
        return
    if message.channel.id == _COMMANDS_CHANNEL_ID:
        await message.delete()


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    await on_chat_edit(after)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    await on_chat_delete(payload)
