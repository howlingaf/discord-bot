import discord
from aiohttp import ClientSession

from .config import (
    GUILD_ID,
    LEETCODE_BASE,
    LEETCODE_SUBMISSIONS_URL,
    LEETCODE_RECAP_CHANNEL_ID,
    STREAMER_NAME,
)
from .database import leetcode_get_problem_by_slug, leetcode_save_problem
from .leetcode import (
    DIFF_COLORS,
    DIFF_EMOJI,
    get_or_create_problem_post,
    fetch_leetcode_problem,
)


async def fetch_streamer_submissions(
    session: ClientSession, stream_start: int, stream_end: int
) -> list[dict]:
    """Fetch streamer's LeetCode submissions within the stream window."""
    async with session.get(LEETCODE_SUBMISSIONS_URL) as resp:
        if resp.status != 200:
            print(f"[RECAP] Failed to fetch streamer submissions: HTTP {resp.status}")
            return []
        data = await resp.json()

    submissions = data if isinstance(data, list) else data.get("submissions") or data.get("submission") or []
    results = []
    for sub in submissions:
        ts = int(sub.get("timestamp") or 0)
        if stream_start <= ts <= stream_end:
            results.append(sub)
    return results


async def resolve_slug_to_question_id(
    session: ClientSession, slug: str
) -> str | None:
    """Resolve a problem slug to its frontend question ID.

    Checks the DB first, then falls back to the API.
    """
    existing = leetcode_get_problem_by_slug(slug)
    if existing:
        return existing["question_id"]

    # Fallback: fetch from API using the /problem/{slug} endpoint
    # The API also accepts slugs and returns questionFrontendId
    url = f"https://leetcode-api-pied.vercel.app/problem/{slug}"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"[RECAP] Failed to resolve slug '{slug}': HTTP {resp.status}")
                return None
            data = await resp.json()
            qid = str(data.get("questionFrontendId") or data.get("questionId") or "")
            if not qid:
                return None
            return qid
    except Exception as e:
        print(f"[RECAP] Error resolving slug '{slug}': {e}")
        return None


async def process_recap(bot, payload: dict):
    """Main recap orchestrator.

    1. Fetch streamer submissions in stream window
    2. Merge with chatter submissions, grouped by slug
    3. For each problem: get or create forum post
    4. Reply on each forum post with submission links + credits
    5. Post recap embed in recap channel
    """
    stream_start = int(payload.get("stream_start") or 0)
    stream_end = int(payload.get("stream_end") or 0)
    stream_problems = payload.get("stream_problems") or []
    chatter_submissions = payload.get("chatter_submissions") or []
    streamer_links = payload.get("streamer_links") or []

    if not bot.http_session:
        print("[RECAP] Bot HTTP session not ready")
        return

    session: ClientSession = bot.http_session

    # Build initial problem list from stream tracking + chatter submissions
    problem_slugs: list[str] = list(stream_problems)
    for cs in chatter_submissions:
        slug = cs.get("slug") or ""
        if slug and slug not in problem_slugs:
            problem_slugs.append(slug)

    # Always fetch streamer submissions from API so none are missed
    streamer_subs = await fetch_streamer_submissions(session, stream_start, stream_end)

    # Merge with any explicitly provided submissions from the payload
    if payload.get("streamer_submissions"):
        seen_ids = {str(s.get("id")) for s in streamer_subs if s.get("id")}
        for sub in payload["streamer_submissions"]:
            if str(sub.get("id") or "") not in seen_ids:
                streamer_subs.append(sub)

    print(f"[RECAP] Found {len(streamer_subs)} streamer submissions in window")

    # Auto-include problems the streamer got accepted during the stream
    for sub in streamer_subs:
        slug = sub.get("titleSlug") or ""
        status = (sub.get("statusDisplay") or "").lower()
        if slug and slug not in problem_slugs and status == "accepted":
            problem_slugs.append(slug)
            stream_problems.append(slug)

    if not problem_slugs and not streamer_links:
        print("[RECAP] No stream problems or links to recap")
        return

    print(f"[RECAP] Problems to recap: {problem_slugs}")

    # Pick best streamer submission per problem:
    # prefer last accepted, otherwise last submission
    streamer_by_slug: dict[str, dict] = {}
    for sub in streamer_subs:
        slug = sub.get("titleSlug") or ""
        if not slug or slug not in problem_slugs:
            continue
        existing = streamer_by_slug.get(slug)
        if not existing:
            streamer_by_slug[slug] = sub
        else:
            sub_accepted = (sub.get("statusDisplay") or "").lower() == "accepted"
            existing_accepted = (existing.get("statusDisplay") or "").lower() == "accepted"
            if sub_accepted and not existing_accepted:
                streamer_by_slug[slug] = sub
            elif sub_accepted == existing_accepted:
                if int(sub.get("timestamp") or 0) > int(existing.get("timestamp") or 0):
                    streamer_by_slug[slug] = sub

    # Group by slug
    by_slug: dict[str, dict] = {}
    for slug in problem_slugs:
        by_slug[slug] = {"streamer": streamer_by_slug.get(slug), "chatters": []}

    for cs in chatter_submissions:
        slug = cs.get("slug") or ""
        if slug in by_slug:
            by_slug[slug]["chatters"].append(cs)

    # 3 & 4. For each problem, get/create forum post and reply
    recap_entries = []  # for the recap message

    for slug, entries in by_slug.items():
        # Resolve slug to question ID
        question_id = await resolve_slug_to_question_id(session, slug)
        if not question_id:
            print(f"[RECAP] Could not resolve slug '{slug}', skipping")
            continue

        # Get or create forum post
        thread_id, err = await get_or_create_problem_post(bot, question_id)
        if not thread_id:
            print(f"[RECAP] Could not get/create post for '{slug}': {err}")
            continue

        thread = bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await bot.fetch_channel(thread_id)
            except Exception as e:
                print(f"[RECAP] Could not fetch thread {thread_id}: {e}")
                continue

        # Build reply content — use <url> to suppress embed previews
        lines = []

        sub = entries["streamer"]
        if sub and (sub.get("statusDisplay") or "").lower() == "accepted":
            sub_id = sub.get("id") or ""
            sub_url = f"{LEETCODE_BASE}/problems/{slug}/submissions/{sub_id}/" if sub_id else ""
            line = f"**{STREAMER_NAME}** submitted a solution!"
            if sub_url:
                line += f"\n<{sub_url}>"
            lines.append(line)

        for cs in entries["chatters"]:
            twitch_user = cs.get("twitch_user") or "anonymous"
            url = cs.get("url") or ""
            line = f"**{twitch_user}** submitted a solution!"
            if url:
                line += f"\n<{url}>"
            lines.append(line)

        if lines:
            content = "\n\n".join(lines)
            if len(content) > 2000:
                content = content[:1997] + "..."
            try:
                # twitch_user is supplied by chatters — suppress mentions.
                await thread.send(content, allowed_mentions=discord.AllowedMentions.none())
                print(f"[RECAP] Posted solutions on thread {thread_id} for '{slug}'")
            except Exception as e:
                print(f"[RECAP] Failed to send to thread {thread_id}: {e}")

        # Only include streamer's problems in the recap embed
        if slug in stream_problems:
            problem_name = slug.replace("-", " ").title()
            recap_entries.append({
                "slug": slug,
                "problem_name": problem_name,
                "question_id": question_id,
                "thread_id": thread_id,
            })

    # 5. Post recap embed
    if recap_entries or streamer_links:
        await _post_recap_message(bot, recap_entries, streamer_links)


async def _post_recap_message(bot, entries: list[dict], streamer_links: list[str]):
    """Build and send the recap embed in the recap channel."""
    channel = bot.get_channel(LEETCODE_RECAP_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(LEETCODE_RECAP_CHANNEL_ID)
        except Exception as e:
            print(f"[RECAP] Could not fetch recap channel: {e}")
            return

    problem_lines = []
    for entry in entries:
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{entry['thread_id']}"
        problem_lines.append(
            f"[{entry['question_id']}. {entry['problem_name']}]({thread_url})"
        )

    if problem_lines:
        embed = discord.Embed(
            title="Stream Recap",
            description="\n\n".join(problem_lines),
            color=0xFFA116,
        )
        try:
            await channel.send(embed=embed)
            print(f"[RECAP] Recap message sent to channel {LEETCODE_RECAP_CHANNEL_ID}")
        except Exception as e:
            print(f"[RECAP] Failed to send recap message: {e}")

    # Send streamer-shared links in their own message(s) so Discord auto-previews
    # them. Discord caps preview embeds at 5 per message, so chunk accordingly.
    if streamer_links:
        for i in range(0, len(streamer_links), 5):
            chunk = streamer_links[i : i + 5]
            content = ("**Links shared**\n" if i == 0 else "") + "\n".join(chunk)
            try:
                await channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                print(f"[RECAP] Failed to send links message: {e}")
                break
        print(f"[RECAP] Sent {len(streamer_links)} streamer link(s) to channel {LEETCODE_RECAP_CHANNEL_ID}")
