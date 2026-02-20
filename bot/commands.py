import asyncio

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
)
from .spotify import dm_spotify_link
from .leetcode import (
    post_leetcode_contest,
    post_leetcode_problem,
    post_leetcode_weekly_premium,
    get_or_create_problem_post,
    get_or_create_problem_post_archived,
    fetch_zerotrac_ratings,
    get_zerotrac_data,
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
    leetcode_contest_posts_delete_by_type,
    linked_users_get,
    linked_users_get_by_username,
    linked_users_set,
    linked_users_delete,
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


async def _run_wipe(log_channel: discord.abc.Messageable, forum_channel: discord.ForumChannel, contest_type: str):
    """Background task: delete all threads in a contest forum and clear DB records."""
    deleted = failed = 0

    # Active threads
    for thread in list(forum_channel.threads):
        try:
            await thread.delete()
            deleted += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[WIPE] Failed to delete thread {thread.id}: {e}")
            failed += 1

    # Archived threads
    try:
        async for thread in forum_channel.archived_threads(limit=None):
            try:
                await thread.delete()
                deleted += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[WIPE] Failed to delete archived thread {thread.id}: {e}")
                failed += 1
    except Exception as e:
        print(f"[WIPE] Error iterating archived threads: {e}")

    removed = leetcode_contest_posts_delete_by_type(contest_type)
    await log_channel.send(
        f"\u2705 Wipe complete: {deleted} Discord threads deleted, {failed} failed, {removed} DB records cleared."
    )


@bot.tree.command(name="wipe-contest-forum", description="(Admin) Delete all threads in a contest forum and clear DB records.")
@app_commands.describe(contest_type="Which forum to wipe: 'weekly' or 'biweekly'")
@app_commands.checks.has_permissions(manage_messages=True)
async def wipe_contest_forum(interaction: discord.Interaction, contest_type: str):
    if contest_type not in ("weekly", "biweekly"):
        await interaction.response.send_message("\u274c Must be 'weekly' or 'biweekly'.", ephemeral=True)
        return

    forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type, 0)
    try:
        forum_channel = await bot.fetch_channel(forum_channel_id)
    except Exception as e:
        await interaction.response.send_message(f"\u274c Could not fetch forum channel: {e}", ephemeral=True)
        return

    if not isinstance(forum_channel, discord.ForumChannel):
        await interaction.response.send_message("\u274c Not a forum channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(_run_wipe(interaction.channel, forum_channel, contest_type))
    await interaction.followup.send(
        f"\u23f3 Wiping {contest_type} forum in the background. Results will appear in this channel.",
        ephemeral=True,
    )


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

    def _sort_key(slug: str) -> tuple[str, int]:
        ctype = _classify_contest(slug) or "z"
        try:
            num = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            num = 0
        return (ctype, num)

    contest_slugs = sorted(contests_map.keys(), key=_sort_key)
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


# ---- Virtual rating system ----

_DEFAULT_RATING = 1500.0
_CONTEST_STRETCH = 50  # serve contests slightly above current rating


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
    embed.add_field(name="Rating", value=f"`{stats['rating']:.0f}`", inline=True)
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

    done_slugs = virtual_contest_history_done_slugs(interaction.user.id)
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

    # Prefer contests within [rating, rating+50]; fall back to closest above; then closest overall
    band = [(s, a) for s, a in candidates if user_rating <= a <= user_rating + _CONTEST_STRETCH]
    if band:
        best_slug, best_avg = min(band, key=lambda x: abs(x[1] - (user_rating + _CONTEST_STRETCH / 2)))
    else:
        above = [(s, a) for s, a in candidates if a > user_rating]
        pool = above if above else candidates
        best_slug, best_avg = min(pool, key=lambda x: abs(x[1] - user_rating))

    # Get or create the contest forum thread
    post = leetcode_contest_post_get(best_slug)
    if post:
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{post['thread_id']}"
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
            leetcode_contest_post_save(best_slug, contest_type_str, thread.id, rated=1 if tag_name != "Unrated" else 0)
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread.id}"
        except Exception as e:
            await interaction.followup.send(f"❌ Could not create contest post: {e}", ephemeral=True)
            return

    virtual_contest_history_log(interaction.user.id, best_slug, user_rating)
    virtual_stats_set_last_contest(interaction.user.id, best_slug)

    contest_label = best_slug.replace("-", " ").title()
    embed = discord.Embed(
        title=f"🏆 {contest_label}",
        description=f"Avg rating: ||{best_avg:.0f}|| | Your rating: `{user_rating:.0f}`\n\n👉 {thread_url}",
        color=0x9B59B6,
    )
    embed.set_footer(text="Log your result with /set-contest <new_rating>")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="set-contest", description="Log your result for the last virtual contest you were served.")
@app_commands.describe(new_rating="Your new rating shown by LeetCode after the virtual contest")
async def set_contest_cmd(interaction: discord.Interaction, new_rating: float):
    await interaction.response.defer(ephemeral=True)

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

    virtual_contest_history_complete(interaction.user.id, slug, new_rating)
    old_rating = stats["rating"]
    virtual_stats_update_rating(interaction.user.id, new_rating)
    delta = new_rating - old_rating
    sign = "+" if delta >= 0 else ""
    await interaction.followup.send(
        f"✅ `{slug.replace('-', ' ').title()}`: `{old_rating:.0f}` → `{new_rating:.0f}` ({sign}{delta:.0f})",
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

    done_slugs = virtual_problem_history_done_slugs(interaction.user.id)
    user_rating = stats["rating"]

    candidates = [(e["title_slug"], e["rating"]) for e in zerotrac_entries if e["title_slug"] not in done_slugs]
    if not candidates:
        await interaction.followup.send("❌ You've done every rated problem. Impressive.", ephemeral=True)
        return

    # Prefer problems within [rating, rating+50]; fall back to closest above; then closest overall
    band = [(s, r) for s, r in candidates if user_rating <= r <= user_rating + _CONTEST_STRETCH]
    if band:
        best_slug, best_rating = min(band, key=lambda x: abs(x[1] - (user_rating + _CONTEST_STRETCH / 2)))
    else:
        above = [(s, r) for s, r in candidates if r > user_rating]
        pool = above if above else candidates
        best_slug, best_rating = min(pool, key=lambda x: abs(x[1] - user_rating))

    thread_id = None
    existing = leetcode_get_problem_by_slug(best_slug)
    if existing:
        thread_id = existing["thread_id"]
    else:
        thread_id, _ = await get_or_create_problem_post_archived(bot, best_slug)

    virtual_problem_history_log(interaction.user.id, best_slug)

    title = best_slug.replace("-", " ").title()
    if thread_id:
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
        desc = f"Rating: ||{best_rating:.0f}|| | Your rating: `{user_rating:.0f}`\n\n👉 {thread_url}"
    else:
        desc = f"Rating: ||{best_rating:.0f}|| | Your rating: `{user_rating:.0f}`\n\n👉 https://leetcode.com/problems/{best_slug}/"

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


@bot.tree.command(name="post-setup-info", description="(Admin) Post pinned how-to post in the problems forum channel.")
@app_commands.checks.has_permissions(manage_messages=True)
async def post_setup_info(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        ch = bot.get_channel(1472231552607064144) or await bot.fetch_channel(1472231552607064144)
        if not isinstance(ch, discord.ForumChannel):
            await interaction.followup.send("❌ Not a forum channel.", ephemeral=True)
            return
        result = await ch.create_thread(
            name="How to Post a LeetCode Problem",
            content="To create a post, use the command: **`/problem <number>`**",
        )
        thread = result.thread if hasattr(result, "thread") else result
        await thread.edit(pinned=True, locked=True)
        await interaction.followup.send("✅ Posted.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)


async def _run_archive_old_contests(log_channel):
    for ch_id in [LEETCODE_WEEKLY_FORUM_CHANNEL_ID, LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID]:
        try:
            ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            if not isinstance(ch, discord.ForumChannel):
                await log_channel.send(f"❌ <#{ch_id}> is not a forum channel")
                continue

            active = sorted(ch.threads, key=lambda t: t.id, reverse=True)

            if len(active) <= 1:
                await log_channel.send(f"ℹ️ <#{ch_id}> — nothing to archive")
                continue

            archived = 0
            for thread in active[1:]:
                try:
                    await thread.edit(archived=True)
                    archived += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"[ARCHIVE CONTESTS] failed {thread.id}: {e}")

            await log_channel.send(f"✅ <#{ch_id}> — archived {archived} thread(s), kept 1")
        except Exception as e:
            await log_channel.send(f"❌ <#{ch_id}>: {e}")


@bot.tree.command(name="archive-old-contests", description="(Admin) Archive all contest threads except the most recent in each forum.")
@app_commands.checks.has_permissions(manage_messages=True)
async def archive_old_contests(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    asyncio.create_task(_run_archive_old_contests(interaction.channel))
    await interaction.followup.send("⏳ Archiving old contests in the background. Results will appear in this channel.", ephemeral=True)


@bot.tree.command(name="archive-inactive-problems", description="(Admin) Archive all problem posts with no replies.")
@app_commands.checks.has_permissions(manage_messages=True)
async def archive_inactive_problems(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        ch = bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
        if not isinstance(ch, discord.ForumChannel):
            await interaction.followup.send("❌ Not a forum channel.", ephemeral=True)
            return
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch problems channel: {e}", ephemeral=True)
        return

    asyncio.create_task(_run_archive_inactive(interaction.channel, ch))
    await interaction.followup.send("⏳ Archiving inactive problems in the background. Result will appear in this channel.", ephemeral=True)


async def _run_archive_inactive(log_channel, forum: discord.ForumChannel):
    archived = skipped = failed = 0

    active = [t for t in forum.threads if not t.archived]
    for thread in active:
        try:
            if thread.message_count == 0:
                await thread.edit(archived=True)
                archived += 1
                await asyncio.sleep(0.5)
            else:
                skipped += 1
        except Exception as e:
            print(f"[ARCHIVE PROBLEMS] failed {thread.id}: {e}")
            failed += 1

    await log_channel.send(
        f"✅ Done: {archived} archived, {skipped} kept (have replies), {failed} failed."
    )


# ---- Commands-only channel ----

_COMMANDS_CHANNEL_ID = 1474387868834336880


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id == _COMMANDS_CHANNEL_ID:
        await message.delete()
        await message.channel.send(
            f"{message.author.mention} Please use slash commands here.",
            delete_after=5,
        )
