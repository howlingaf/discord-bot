import asyncio

import discord
from discord import app_commands

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    GUILD_ID,
)
from .spotify import dm_spotify_link
from .leetcode import (
    post_leetcode_contest,
    post_leetcode_problem,
    post_leetcode_weekly_premium,
    get_or_create_problem_post,
    get_or_create_problem_post_archived,
    fetch_zerotrac_ratings,
    build_contest_forum_embed,
    CONTEST_FORUM_CHANNEL_MAP,
    _classify_contest,
    _contest_difficulty_tag,
    _get_or_create_forum_tag,
)
from .database import (
    leetcode_delete_problem,
    leetcode_get_problem_by_slug,
    leetcode_contest_post_get,
    leetcode_contest_post_save,
    linked_users_get,
    linked_users_get_by_username,
    linked_users_set,
    linked_users_delete,
)
from .client import bot


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
        await interaction.followup.send(f"\u274c Invalid problem ID.", ephemeral=True)


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
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="weekly", description="(Admin) Post the current LeetCode weekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "weekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="biweekly", description="(Admin) Post the current LeetCode biweekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def biweekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "biweekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="link", description="Link your Discord account to your LeetCode profile.")
@app_commands.describe(username="Your LeetCode username")
async def link(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Check if this LeetCode username is already claimed by someone else
        existing_owner = linked_users_get_by_username(username)
        if existing_owner and existing_owner != interaction.user.id:
            await interaction.followup.send("\u274c That LeetCode username is already linked to another user.", ephemeral=True)
            return

        # Verify the username exists on LeetCode
        async with bot.http_session.get(f"https://leetcode-api-pied.vercel.app/user/{username}") as resp:
            if resp.status != 200:
                await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
                return
            data = await resp.json()
            if not data.get("username"):
                await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
                return

        linked_users_set(interaction.user.id, username)
        await interaction.followup.send(f"\u2705 Linked to **{username}**.", ephemeral=True)
    except Exception as e:
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
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="backfill-test", description="(Admin) Test backfill with weekly-contest-100 and biweekly-contest-100.")
@app_commands.checks.has_permissions(manage_messages=True)
async def backfill_test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        async with bot.http_session.get(
            "https://raw.githubusercontent.com/zerotrac/leetcode_problem_rating/main/data.json"
        ) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"\u274c Failed to fetch zerotrac data (HTTP {resp.status}).", ephemeral=True)
                return
            data = await resp.json(content_type=None)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed to fetch zerotrac data: {e}", ephemeral=True)
        return

    ratings_by_slug: dict[str, float] = {p["TitleSlug"]: p["Rating"] for p in data}

    from collections import defaultdict
    contests_map: dict[str, list[dict]] = defaultdict(list)
    for p in data:
        contests_map[p["ContestSlug"]].append(p)

    test_slugs = ["weekly-contest-100", "biweekly-contest-100"]
    results: list[str] = []

    for slug in test_slugs:
        if slug not in contests_map:
            results.append(f"\u2753 `{slug}` not found in zerotrac data")
            continue

        if leetcode_contest_post_get(slug):
            results.append(f"\u23ed\ufe0f `{slug}` already exists, skipping")
            continue

        contest_type = _classify_contest(slug)
        forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type, 0)

        try:
            forum_channel = bot.get_channel(forum_channel_id) or await bot.fetch_channel(forum_channel_id)
        except Exception as e:
            results.append(f"\u274c `{slug}` — could not fetch forum channel: {e}")
            continue

        if not isinstance(forum_channel, discord.ForumChannel):
            results.append(f"\u274c `{slug}` — channel is not a forum channel")
            continue

        problems = sorted(contests_map[slug], key=lambda p: p["ProblemIndex"])

        question_thread_ids: dict[str, int] = {}
        for p in problems:
            p_slug = p["TitleSlug"]
            try:
                thread_id, err = await get_or_create_problem_post_archived(bot, p_slug)
                if thread_id:
                    question_thread_ids[p_slug] = thread_id
                elif err:
                    print(f"[BACKFILL TEST] problem post '{p_slug}': {err}")
            except Exception as e:
                print(f"[BACKFILL TEST] problem post '{p_slug}' failed: {e}")

        contest_id_en = problems[0].get("ContestID_en", slug.replace("-", " ").title())
        mock_contest = {"title": contest_id_en, "titleSlug": slug, "startTime": 0}
        questions = [
            {"questionId": str(p["ID"]), "title": p["Title"], "titleSlug": p["TitleSlug"]}
            for p in problems
        ]

        try:
            tag_name = _contest_difficulty_tag(questions, ratings_by_slug)
            contest_tag = await _get_or_create_forum_tag(forum_channel, tag_name)
            forum_embed = build_contest_forum_embed(mock_contest, questions, ratings_by_slug, question_thread_ids)
            result = await forum_channel.create_thread(
                name=contest_id_en[:100],
                embed=forum_embed,
                applied_tags=[contest_tag],
                reason=f"Backfill test: {slug}",
            )
            thread = result.thread if hasattr(result, "thread") else result
            is_rated = tag_name != "Unrated"
            leetcode_contest_post_save(slug, contest_type, thread.id, rated=1 if is_rated else 0)
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread.id}"
            results.append(f"\u2705 `{slug}` — [{tag_name}] [forum post]({thread_url}), {len(question_thread_ids)}/{len(problems)} problem posts created")
        except Exception as e:
            results.append(f"\u274c `{slug}` — forum thread failed: {e}")

    await interaction.followup.send("\n".join(results) or "No results.", ephemeral=True)


async def _run_backfill(log_channel: discord.TextChannel, data: list[dict]):
    """Background task: backfill all contests. Posts progress to log_channel."""
    from collections import defaultdict

    ratings_by_slug: dict[str, float] = {p["TitleSlug"]: p["Rating"] for p in data}

    contests_map: dict[str, list[dict]] = defaultdict(list)
    for p in data:
        contests_map[p["ContestSlug"]].append(p)

    # Pre-fetch forum channels and ensure all tags exist so we never
    # hit a channel-edit API call inside the hot loop.
    tag_names = ["Easy", "Medium", "Hard", "Unrated"]
    channel_cache: dict[str, discord.ForumChannel] = {}
    for ctype, fid in CONTEST_FORUM_CHANNEL_MAP.items():
        try:
            ch = await bot.fetch_channel(fid)
            if isinstance(ch, discord.ForumChannel):
                existing = {t.name for t in ch.available_tags}
                missing = [discord.ForumTag(name=n) for n in tag_names if n not in existing]
                if missing:
                    ch = await ch.edit(available_tags=list(ch.available_tags) + missing)
                channel_cache[ctype] = ch
        except Exception as e:
            print(f"[BACKFILL] could not set up forum channel for {ctype}: {e}")

    contest_slugs = sorted(contests_map.keys())
    total = len(contest_slugs)
    created = skipped = failed = 0

    for i, slug in enumerate(contest_slugs):
        if leetcode_contest_post_get(slug):
            skipped += 1
            continue

        contest_type = _classify_contest(slug)
        if not contest_type or contest_type not in channel_cache:
            skipped += 1
            continue

        forum_channel = channel_cache[contest_type]
        problems = sorted(contests_map[slug], key=lambda p: p["ProblemIndex"])

        # Resolve problem thread IDs — only hit the API if the problem
        # isn't already in the DB. Sleep only when a real API call is made.
        question_thread_ids: dict[str, int] = {}
        for p in problems:
            p_slug = p["TitleSlug"]
            try:
                existing = leetcode_get_problem_by_slug(p_slug)
                if existing:
                    question_thread_ids[p_slug] = existing["thread_id"]
                else:
                    thread_id, err = await get_or_create_problem_post_archived(bot, p_slug)
                    if thread_id:
                        question_thread_ids[p_slug] = thread_id
                    elif err:
                        print(f"[BACKFILL] problem post '{p_slug}': {err}")
                    await asyncio.sleep(4.0)  # only sleep when we hit the API
            except Exception as e:
                print(f"[BACKFILL] problem post '{p_slug}' failed: {e}")

        contest_id_en = problems[0].get("ContestID_en", slug.replace("-", " ").title())
        mock_contest = {"title": contest_id_en, "titleSlug": slug, "startTime": 0}
        questions = [
            {"questionId": str(p["ID"]), "title": p["Title"], "titleSlug": p["TitleSlug"]}
            for p in problems
        ]

        try:
            tag_name = _contest_difficulty_tag(questions, ratings_by_slug)
            contest_tag = next(t for t in forum_channel.available_tags if t.name == tag_name)
            forum_embed = build_contest_forum_embed(mock_contest, questions, ratings_by_slug, question_thread_ids)
            result = await forum_channel.create_thread(
                name=contest_id_en[:100],
                embed=forum_embed,
                applied_tags=[contest_tag],
                reason=f"Backfill: {slug}",
            )
            thread = result.thread if hasattr(result, "thread") else result
            leetcode_contest_post_save(slug, contest_type, thread.id, rated=1 if tag_name != "Unrated" else 0)
            created += 1
            await asyncio.sleep(4.0)  # only sleep when we hit the API
        except Exception as e:
            print(f"[BACKFILL] contest thread '{slug}' failed: {e}")
            failed += 1

        if (i + 1) % 25 == 0:
            try:
                await log_channel.send(
                    f"\u23f3 Backfill progress: {i + 1}/{total} ({created} created, {skipped} skipped, {failed} failed)"
                )
            except Exception:
                pass

    try:
        await log_channel.send(
            f"\u2705 Backfill complete: {created} created, {skipped} skipped, {failed} failed (of {total} total)."
        )
    except Exception:
        pass


@bot.tree.command(name="backfill-contests", description="(Admin) Backfill all past contests from zerotrac into forum channels.")
@app_commands.checks.has_permissions(manage_messages=True)
async def backfill_contests(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        async with bot.http_session.get(
            "https://raw.githubusercontent.com/zerotrac/leetcode_problem_rating/main/data.json"
        ) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"\u274c Failed to fetch zerotrac data (HTTP {resp.status}).", ephemeral=True)
                return
            data = await resp.json(content_type=None)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed to fetch zerotrac data: {e}", ephemeral=True)
        return

    log_channel = interaction.channel
    asyncio.create_task(_run_backfill(log_channel, data))
    await interaction.followup.send(
        f"\u23f3 Backfill started ({sum(1 for p in {p['ContestSlug'] for p in data})} contests). "
        f"Progress updates will appear in this channel.",
        ephemeral=True,
    )
