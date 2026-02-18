import asyncio
import html
import re
from datetime import datetime

import discord
from aiohttp import ClientSession

from .config import (
    LEETCODE_DAILY_URL,
    LEETCODE_PROBLEM_URL,
    LEETCODE_BASE,
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_DAILY_NOTIF_CHANNEL_ID,
    MAX_EXAMPLES,
    LEETCODE_WEEKLY_CHANNEL_ID,
    LEETCODE_BIWEEKLY_CHANNEL_ID,
    LEETCODE_CONTEST_URL,
    GUILD_ID,
)
from .database import (
    leetcode_get_problem,
    leetcode_save_problem,
    leetcode_get_daily_state,
    leetcode_set_daily_state,
    leetcode_get_contest_state,
    leetcode_set_contest_state,
)


# ---------------- Thread helpers ----------------
async def archive_all_threads(
    channel: discord.TextChannel,
    *,
    lock: bool = True,
    reason: str = "Archiving old threads",
):
    threads: list[discord.Thread] = []
    try:
        threads = list(await channel.active_threads())
    except Exception:
        threads = list(getattr(channel, "threads", []))

    for t in threads:
        if isinstance(t, discord.Thread) and not t.archived:
            try:
                await t.edit(archived=True, locked=lock, reason=reason)
            except discord.Forbidden:
                print(f"[ARCHIVE] Forbidden archiving thread {t.id} ({t.name})")
            except discord.HTTPException as e:
                print(f"[ARCHIVE] HTTPException archiving thread {t.id} ({t.name}): {e}")


# ---------------- Formatting helpers ----------------
def _clean_zw(text: str) -> str:
    return (
        text.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
    )


def html_to_text_preserve_newlines(content_html: str) -> str:
    s = content_html or ""
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</li\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</h\d\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<(script|style)[\s\S]*?</\1>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = _clean_zw(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def extract_pre_blocks(content_html: str) -> list[str]:
    blocks = re.findall(r"<pre[\s\S]*?</pre>", content_html or "", flags=re.IGNORECASE)
    out: list[str] = []
    for b in blocks:
        b = re.sub(r"^<pre[^>]*>", "", b.strip(), flags=re.IGNORECASE)
        b = re.sub(r"</pre>$", "", b.strip(), flags=re.IGNORECASE)
        b = re.sub(r"<[^>]+>", "", b)
        b = html.unescape(b)
        b = _clean_zw(b)
        b = b.replace("\r\n", "\n").replace("\r", "\n").strip()
        if b:
            out.append(b)
    return out


def extract_example_blocks(content_html: str) -> list[str]:
    """Extract examples from <div class="example-block"> sections."""
    blocks = re.findall(r'<div\s+class="example-block"[\s\S]*?</div>', content_html or "", flags=re.IGNORECASE)
    out: list[str] = []
    for b in blocks:
        b = re.sub(r"<table[\s\S]*?</table>", "", b, flags=re.IGNORECASE)
        b = re.sub(r"<[^>]+>", "", b)
        b = html.unescape(b)
        b = _clean_zw(b)
        b = b.replace("\r\n", "\n").replace("\r", "\n")
        b = re.sub(r"\n{2,}", "\n", b).strip()
        if b:
            out.append(b)
    return out


def split_statement_and_constraints(plain_text: str) -> tuple[str, str]:
    m = re.search(r"\bConstraints\s*:\s*", plain_text, flags=re.IGNORECASE)
    if not m:
        return plain_text.strip(), ""
    stmt = plain_text[:m.start()].strip()
    cons = plain_text[m.end():].strip()
    return stmt, cons


def chunk_text(text: str, max_len: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    paras = text.split("\n\n")
    chunks: list[str] = []
    cur = ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        candidate = (cur + ("\n\n" if cur else "") + p)
        if len(candidate) <= max_len:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            while len(p) > max_len:
                chunks.append(p[:max_len])
                p = p[max_len:]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def format_leetcode_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%B %-d, %Y")  # Linux/mac
    except ValueError:
        return date_str
    except Exception:
        # Windows uses %#d instead of %-d
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%B %#d, %Y")
        except Exception:
            return date_str


DIFF_COLORS = {"Easy": 0x00b8a3, "Medium": 0xffc01e, "Hard": 0xff375f}
DIFF_EMOJI = {"Easy": "\U0001f7e2", "Medium": "\U0001f7e1", "Hard": "\U0001f534"}


def _find_forum_tags(forum: discord.ForumChannel, names: list[str]) -> list[discord.ForumTag]:
    name_set = {n.lower() for n in names}
    return [t for t in forum.available_tags if t.name.lower() in name_set]


async def _get_or_create_forum_tag(forum: discord.ForumChannel, name: str) -> discord.ForumTag:
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    updated = await forum.edit(available_tags=list(forum.available_tags) + [discord.ForumTag(name=name)])
    for tag in updated.available_tags:
        if tag.name == name:
            return tag
    raise RuntimeError(f"Could not create forum tag '{name}'")


def build_daily_embeds(daily: dict) -> list[discord.Embed]:
    link = daily.get("link") or ""
    q = daily.get("question") or {}
    qid = q.get("questionFrontendId") or q.get("questionId") or ""
    title = f"{qid}. {q.get('title') or 'LeetCode Daily'}" if qid else (q.get("title") or "LeetCode Daily")
    difficulty = q.get("difficulty") or "Unknown"
    url = f"{LEETCODE_BASE}{link}" if link.startswith("/") else (link or LEETCODE_BASE)

    content_html = q.get("content") or ""

    examples = extract_pre_blocks(content_html)[:max(0, MAX_EXAMPLES)]
    if not examples:
        examples = extract_example_blocks(content_html)[:max(0, MAX_EXAMPLES)]

    statement_html = re.sub(r"<pre[\s\S]*?</pre>", "", content_html, flags=re.IGNORECASE)
    statement_html = re.sub(r'<div\s+class="example-block"[\s\S]*?</div>', "", statement_html, flags=re.IGNORECASE)
    plain = html_to_text_preserve_newlines(statement_html)
    statement, constraints = split_statement_and_constraints(plain)

    statement = re.sub(r"Example\s+\d+:\s*", "", statement).strip()
    statement = re.sub(r"Follow\s+up:[\s\S]*", "", statement, flags=re.IGNORECASE).strip()

    diff_emoji = DIFF_EMOJI.get(difficulty, "\u26aa")
    color = DIFF_COLORS.get(difficulty, 0x808080)

    embeds: list[discord.Embed] = []

    header_line = f"{diff_emoji} **{difficulty}**"

    desc_parts = [header_line, ""]

    if statement:
        trimmed = statement[:2800]
        if len(statement) > 2800:
            trimmed += "\n*(...continued on LeetCode)*"
        desc_parts.append(trimmed)

    main_embed = discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_parts),
        color=color,
    )
    embeds.append(main_embed)

    if constraints:
        lines = [ln.strip() for ln in constraints.split("\n") if ln.strip()]
        bullet = "\n".join(f"\u2022 `{ln}`" for ln in lines) if lines else constraints

        if len(bullet) > 4000:
            bullet = bullet[:3997] + "..."

        embeds.append(discord.Embed(
            title="Constraints",
            description=bullet,
            color=color,
        ))

    for i, ex in enumerate(examples, start=1):
        ex_text = ex.strip()
        if len(ex_text) > 1200:
            ex_text = ex_text[:1200] + "\n..."
        embeds.append(discord.Embed(
            title=f"Example {i}",
            description=f"```\n{ex_text}\n```",
            color=color,
        ))

    return embeds[:10]


async def fetch_leetcode_daily(session: ClientSession) -> dict:
    async with session.get(LEETCODE_DAILY_URL) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"LeetCode daily API failed: {resp.status} {js}")
        return js


async def fetch_leetcode_problem(session: ClientSession, question_id: str) -> dict:
    """Fetch full problem details by frontend ID."""
    url = LEETCODE_PROBLEM_URL.format(qid=question_id)
    async with session.get(url) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"LeetCode problem API failed: {resp.status} {js}")
        # The /problem endpoint doesn't return titleSlug directly,
        # but provides a full url like "https://leetcode.com/problems/reverse-bits/"
        problem_url = js.get("url") or ""
        slug_match = re.search(r"/problems/([^/]+)", problem_url)
        title_slug = slug_match.group(1) if slug_match else ""

        return {
            "question": {
                "questionFrontendId": js.get("questionFrontendId") or js.get("questionId") or "",
                "title": js.get("title") or "",
                "titleSlug": title_slug,
                "difficulty": js.get("difficulty") or "Unknown",
                "isPaidOnly": bool(js.get("isPaidOnly") or js.get("paidOnly")),
                "content": js.get("content") or "",
            },
            "link": f"/problems/{title_slug}/" if title_slug else "",
        }


async def get_or_create_problem_post(bot, question_id: str) -> tuple[int | None, str]:
    """Look up or create a forum post for the given question ID.

    Returns (thread_id, error_message). thread_id is None on failure.
    """
    if not bot.http_session:
        return None, "http session not ready"

    # Check DB first
    existing = leetcode_get_problem(question_id)
    if existing:
        return existing["thread_id"], ""

    # Fetch problem details
    data = await fetch_leetcode_problem(bot.http_session, question_id)
    q = data["question"]
    qid = q["questionFrontendId"]
    qtitle = q["title"]
    title_slug = q["titleSlug"]

    if not qtitle:
        return None, f"could not find LeetCode problem #{question_id}"

    # Create forum post
    forum = bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
    if not isinstance(forum, discord.ForumChannel):
        return None, "problems channel is not a forum channel"

    thread_name = f"{qid}. {qtitle}".strip(". ")[:100] if qid else qtitle[:100]
    embeds = build_daily_embeds(data)

    tag_names = [q.get("difficulty") or ""]
    if q.get("isPaidOnly"):
        tag_names.append("Premium")
    tags = _find_forum_tags(forum, tag_names)

    result = await forum.create_thread(
        name=thread_name,
        embeds=embeds,
        applied_tags=tags,
        reason=f"Problem post for #{qid}",
    )
    thread = result.thread if hasattr(result, "thread") else result

    leetcode_save_problem(
        question_id=qid,
        title_slug=title_slug,
        title=qtitle,
        thread_id=thread.id,
        difficulty=q.get("difficulty"),
    )
    return thread.id, ""


async def post_leetcode_problem(bot, *, force: bool = False) -> tuple[bool, str]:
    if not bot.http_session:
        return False, "http session not ready"

    daily = await fetch_leetcode_daily(bot.http_session)
    date = daily.get("date") or ""
    q = (daily.get("question") or {})
    title_slug = q.get("titleSlug") or ""
    qid = q.get("questionFrontendId") or q.get("questionId") or ""
    qtitle = q.get("title") or "LeetCode Daily"

    # Convert date string to unix timestamp
    date_ts = 0
    if date:
        try:
            date_ts = int(datetime.strptime(date, "%Y-%m-%d").timestamp())
        except ValueError:
            pass

    # Capture old daily's thread_id for tag swap later
    old_daily = leetcode_get_daily_state()
    old_thread_id: int | None = None
    if old_daily and old_daily.get("question_id") != qid:
        old_problem = leetcode_get_problem(old_daily["question_id"])
        if old_problem:
            old_thread_id = old_problem["thread_id"]

    # Check if we already sent the notification for today's daily
    if not force:
        state = leetcode_get_daily_state()
        if state and state["date"] == date_ts:
            return False, f"already posted for date={date}"

    # --- Forum post (look up DB, then create if needed) ---
    forum = bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
    if not isinstance(forum, discord.ForumChannel):
        return False, "LEETCODE_PROBLEMS_CHANNEL_ID must be a forum channel"

    daily_tag: discord.ForumTag | None = None
    try:
        daily_tag = await _get_or_create_forum_tag(forum, "Daily")
    except Exception as e:
        print(f"[DAILY TAG] Could not get/create tag: {e}")

    thread_name = f"{qid}. {qtitle}".strip(". ")[:100] if qid else qtitle[:100]

    existing = leetcode_get_problem(qid)
    thread_id = existing["thread_id"] if existing else None

    if thread_id is None:
        embeds = build_daily_embeds(daily)
        try:
            tag_names = [q.get("difficulty") or ""]
            if q.get("isPaidOnly"):
                tag_names.append("Premium")
            tags = _find_forum_tags(forum, tag_names)
            if daily_tag:
                tags = [t for t in tags if t.id != daily_tag.id] + [daily_tag]
            result = await forum.create_thread(
                name=thread_name,
                embeds=embeds,
                applied_tags=tags,
                reason="Daily LeetCode discussion post",
            )
            thread = result.thread if hasattr(result, "thread") else result
            thread_id = thread.id
            leetcode_save_problem(
                question_id=qid,
                title_slug=title_slug,
                title=qtitle,
                thread_id=thread_id,
                difficulty=q.get("difficulty"),
            )
        except Exception as e:
            print("[PROBLEM] forum post create failed:", repr(e))
    elif daily_tag and thread_id:
        # Thread already exists — apply the Daily tag if not already present
        try:
            existing_thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(existing_thread, discord.Thread):
                if not any(t.id == daily_tag.id for t in existing_thread.applied_tags):
                    await existing_thread.edit(applied_tags=list(existing_thread.applied_tags) + [daily_tag])
        except Exception as e:
            print(f"[DAILY TAG] Failed to apply tag to thread {thread_id}: {e}")

    # Remove Daily tag from previous daily's thread
    if daily_tag and old_thread_id and old_thread_id != thread_id:
        try:
            old_thread = bot.get_channel(old_thread_id) or await bot.fetch_channel(old_thread_id)
            if isinstance(old_thread, discord.Thread):
                new_applied = [t for t in old_thread.applied_tags if t.id != daily_tag.id]
                await old_thread.edit(applied_tags=new_applied)
        except Exception as e:
            print(f"[DAILY TAG] Failed to remove tag from old thread {old_thread_id}: {e}")

    # --- Notification card in text channel ---
    try:
        notif_channel = bot.get_channel(LEETCODE_DAILY_NOTIF_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_DAILY_NOTIF_CHANNEL_ID)

        difficulty = q.get("difficulty") or "Unknown"
        diff_emoji = DIFF_EMOJI.get(difficulty, "\u26aa")
        color = DIFF_COLORS.get(difficulty, 0x808080)
        pretty_date = format_leetcode_date(date) if date else ""
        url = f"{LEETCODE_BASE}/problems/{title_slug}/" if title_slug else LEETCODE_BASE

        notif_title = f"{qid}. {qtitle}" if qid else qtitle

        desc_lines = [f"{diff_emoji} **{difficulty}**"]

        # Problem statement – strip examples, constraints, follow-up
        content_html = q.get("content") or ""
        statement_html = re.sub(r"<pre[\s\S]*?</pre>", "", content_html, flags=re.IGNORECASE)
        statement_html = re.sub(r"<div\s+class=\"example-block\"[\s\S]*?</div>", "", statement_html, flags=re.IGNORECASE)
        plain = html_to_text_preserve_newlines(statement_html)
        statement, _ = split_statement_and_constraints(plain)
        statement = re.sub(r"Example\s+\d+:\s*", "", statement).strip()
        statement = re.sub(r"Follow\s+up:[\s\S]*", "", statement, flags=re.IGNORECASE).strip()
        if statement:
            trimmed = statement[:1500]
            if len(statement) > 1500:
                trimmed += "\n*(...continued)*"
            desc_lines.append(f"\n{trimmed}")

        if thread_id:
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            desc_lines.append(f"\n\U0001f449 [View Post]({thread_url})")

        notif_embed = discord.Embed(
            title=notif_title,
            url=url,
            description="\n".join(desc_lines),
            color=color,
        )

        await notif_channel.send(embed=notif_embed)
    except Exception as e:
        print("[PROBLEM] notification send failed:", repr(e))

    leetcode_set_daily_state(
        question_id=qid,
        title_slug=title_slug,
        title=qtitle,
        date=date_ts,
    )
    return True, f"posted {date=} {title_slug=}"



async def leetcode_daily_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(3)
    print("\u2705 LeetCode daily scheduler started (polling every 5 min)")
    while not bot.is_closed():
        try:
            posted, msg = await post_leetcode_problem(bot, force=False)
            if posted:
                print(f"[DAILY] {msg}")
        except Exception as e:
            print("[DAILY] error:", repr(e))

        await asyncio.sleep(300)  # poll every 5 minutes


# ------------------------------------------------------------------ #
#  LeetCode Contest (weekly / biweekly)                               #
# ------------------------------------------------------------------ #

CONTEST_COLOR = 0xFFA116  # LeetCode orange

CONTEST_CHANNEL_MAP: dict[str, int] = {
    "weekly": LEETCODE_WEEKLY_CHANNEL_ID,
    "biweekly": LEETCODE_BIWEEKLY_CHANNEL_ID,
}


async def fetch_leetcode_contests(session: ClientSession) -> list[dict]:
    async with session.get(LEETCODE_CONTEST_URL) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"LeetCode contest API failed: {resp.status} {js}")
        return js.get("topTwoContests") or []


def _classify_contest(title_slug: str) -> str | None:
    if title_slug.startswith("weekly-contest"):
        return "weekly"
    if title_slug.startswith("biweekly-contest"):
        return "biweekly"
    return None


def build_contest_embed(contest: dict) -> discord.Embed:
    title = contest.get("title") or "LeetCode Contest"
    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0
    duration = contest.get("duration") or 5400

    url = f"{LEETCODE_BASE}/contest/{slug}" if slug else LEETCODE_BASE
    dur_min = duration // 60

    desc_lines: list[str] = []
    if start_ts:
        desc_lines.append(f"\U0001f5d3\ufe0f **Start:** <t:{start_ts}:F>")
    desc_lines.append(f"\u23f1\ufe0f **Duration:** {dur_min} minutes")

    embed = discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_lines),
        color=CONTEST_COLOR,
    )
    return embed


CONTEST_LEAD_SECONDS = 2 * 60 * 60  # 2 hours before start


async def post_leetcode_contest(
    bot,
    contest_type: str,
    *,
    force: bool = False,
    contests: list[dict] | None = None,
) -> tuple[bool, str]:
    if not bot.http_session:
        return False, "http session not ready"

    if contests is None:
        contests = await fetch_leetcode_contests(bot.http_session)

    contest = None
    for c in contests:
        if _classify_contest(c.get("titleSlug") or "") == contest_type:
            contest = c
            break

    if not contest:
        return False, f"no {contest_type} contest found in API response"

    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0

    if not force:
        last_slug = leetcode_get_contest_state(contest_type)
        if last_slug == slug:
            return False, f"already posted {contest_type} slug={slug}"

        # Only post within 2 hours of start
        now = int(datetime.now().timestamp())
        if start_ts and now < start_ts - CONTEST_LEAD_SECONDS:
            return False, f"{contest_type} starts <t:{start_ts}:R>, too early to post"

    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} channel must be a text channel"

    # Archive all existing threads before creating the new one
    await archive_all_threads(
        channel,
        lock=True,
        reason=f"New {contest_type} contest starting",
    )

    embed = build_contest_embed(contest)
    sent = await channel.send(embed=embed)

    title = contest.get("title") or contest_type.title()
    thread_name = title[:100]
    try:
        await channel.create_thread(
            name=thread_name,
            message=sent,
            auto_archive_duration=10080,
            reason=f"{contest_type.title()} contest discussion thread",
        )
    except Exception as e:
        print(f"[CONTEST/{contest_type.upper()}] thread create failed:", repr(e))

    leetcode_set_contest_state(contest_type, slug)
    return True, f"posted {contest_type} slug={slug}"


async def leetcode_contest_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    print("\u2705 LeetCode contest scheduler started")
    while not bot.is_closed():
        try:
            contests = await fetch_leetcode_contests(bot.http_session)

            # Try to post any contest that's within the 2hr window now
            for ctype in ("weekly", "biweekly"):
                try:
                    posted, msg = await post_leetcode_contest(
                        bot, ctype, force=False, contests=contests,
                    )
                    if posted:
                        print(f"[CONTEST/{ctype.upper()}] {msg}")
                except Exception as e:
                    print(f"[CONTEST/{ctype.upper()}] error:", repr(e))

            # Find the next contest post time (start - 2hrs) to sleep until
            now = int(datetime.now().timestamp())
            next_post_times = []
            for c in contests:
                start_ts = c.get("startTime") or 0
                post_at = start_ts - CONTEST_LEAD_SECONDS
                if post_at > now:
                    next_post_times.append(post_at)

            if next_post_times:
                wait = min(next_post_times) - now
                print(f"[CONTEST] sleeping {wait}s until next contest post time")
                await asyncio.sleep(wait)
            else:
                # No upcoming contests found, check again in 6 hours
                print("[CONTEST] no upcoming contests, rechecking in 6h")
                await asyncio.sleep(6 * 60 * 60)

        except Exception as e:
            print("[CONTEST] error:", repr(e))
            await asyncio.sleep(60 * 60)  # retry in 1 hour on error
