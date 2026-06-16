import asyncio
import html
import re
from datetime import datetime

import discord
from aiohttp import ClientSession

from .logbus import log_error
from .config import (
    LEETCODE_DAILY_URL,
    LEETCODE_PROBLEM_URL,
    LEETCODE_BASE,
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_DAILY_NOTIF_CHANNEL_ID,
    MAX_EXAMPLES,
    LEETCODE_WEEKLY_CHANNEL_ID,
    LEETCODE_BIWEEKLY_CHANNEL_ID,
    LEETCODE_WEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_PREMIUM_WEEKLY_CHANNEL_ID,
    GUILD_ID,
)
from .database import (
    leetcode_get_problem,
    leetcode_get_problem_by_slug,
    leetcode_save_problem,
    leetcode_get_daily_state,
    leetcode_set_daily_state,
    leetcode_get_contest_state,
    leetcode_set_contest_state,
    leetcode_get_premium_weekly_state,
    leetcode_set_premium_weekly_state,
    leetcode_contest_post_get,
    leetcode_contest_post_save,
    leetcode_contest_post_set_rated,
    leetcode_contest_post_set_rankings_posted,
    leetcode_contest_post_set_problems_posted,
    leetcode_contest_post_set_notif_message_id,
    leetcode_contest_posts_get_unrated,
    leetcode_contest_posts_get_pending_rankings,
    linked_users_all,
    zerotrac_cache_get_all,
    zerotrac_cache_updated_at,
    zerotrac_cache_upsert_all,
)


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
    s = re.sub(r"<sup>(.*?)</sup>", r"^\1", s, flags=re.IGNORECASE)
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
    tag_map = {t.name.lower(): t for t in forum.available_tags}
    return [tag_map[n.lower()] for n in names if n.lower() in tag_map]


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
    if q.get("isPaidOnly"):
        header_line += " \U0001f512 Premium"

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


async def fetch_problem_by_slug_graphql(session: ClientSession, title_slug: str) -> dict:
    """Fetch problem details from LeetCode GraphQL by title slug.

    Used as a fallback when the pied API doesn't have the problem yet
    (e.g. a brand-new contest problem before the contest ends).
    Returns data in the same format as fetch_leetcode_problem.
    """
    csrf = await fetch_leetcode_csrf(session)
    query = """
    query($titleSlug: String!) {
        question(titleSlug: $titleSlug) {
            questionFrontendId
            title
            titleSlug
            difficulty
            isPaidOnly
            content
        }
    }
    """
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com/",
        "x-csrftoken": csrf,
    }
    payload = {"query": query.strip(), "variables": {"titleSlug": title_slug}}
    async with session.post("https://leetcode.com/graphql", json=payload, headers=headers) as resp:
        js = await resp.json(content_type=None)
        q = (js.get("data") or {}).get("question")
        if not q:
            raise RuntimeError(f"Problem '{title_slug}' not found via GraphQL")
        return {
            "question": {
                "questionFrontendId": q.get("questionFrontendId") or "",
                "title": q.get("title") or "",
                "titleSlug": q.get("titleSlug") or title_slug,
                "difficulty": q.get("difficulty") or "Unknown",
                "isPaidOnly": bool(q.get("isPaidOnly")),
                "content": q.get("content") or "",
            },
            "link": f"/problems/{title_slug}/",
        }


async def _create_problem_forum_post(bot, data: dict) -> tuple[int | None, str]:
    """Create a problem forum thread from fetched problem data and save it to the DB."""
    q = data["question"]
    qid = q.get("questionFrontendId") or q.get("questionId") or ""
    qtitle = q.get("title") or ""
    title_slug = q.get("titleSlug") or ""

    if not qtitle:
        return None, "problem has no title"

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


# Per-problem locks so two concurrent callers (e.g. the recap task and the daily
# poller) can't both create a forum thread for the same problem. Keyed by the
# identifier passed in (question id or slug); callers within a feature use a
# consistent key. NOT reentrant — never acquire the same key while already held.
_post_locks: dict[str, asyncio.Lock] = {}


def _post_lock(key: str) -> asyncio.Lock:
    lock = _post_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _post_locks[key] = lock
    return lock


async def get_or_create_problem_post(bot, question_id: str) -> tuple[int | None, str]:
    """Look up or create a forum post for the given question ID.

    Returns (thread_id, error_message). thread_id is None on failure.
    """
    if not bot.http_session:
        return None, "http session not ready"

    # Check DB first (fast path, no lock)
    existing = leetcode_get_problem(question_id)
    if existing:
        return existing["thread_id"], ""

    async with _post_lock(question_id):
        # Re-check inside the lock: another task may have created it meanwhile.
        existing = leetcode_get_problem(question_id)
        if existing:
            return existing["thread_id"], ""

        data = await fetch_leetcode_problem(bot.http_session, question_id)
        if not data["question"].get("title"):
            return None, f"could not find LeetCode problem #{question_id}"

        return await _create_problem_forum_post(bot, data)


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
        log_error(f"[DAILY TAG] Could not get/create tag: {e}")

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
            log_error("[PROBLEM] forum post create failed:", repr(e))
    if daily_tag and thread_id:
        # Apply the Daily tag and unarchive so people can discuss
        try:
            existing_thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(existing_thread, discord.Thread):
                new_tags = list(existing_thread.applied_tags)
                if not any(t.id == daily_tag.id for t in new_tags):
                    new_tags.append(daily_tag)
                await existing_thread.edit(archived=False, applied_tags=new_tags)
        except Exception as e:
            log_error(f"[DAILY TAG] Failed to apply tag to thread {thread_id}: {e}")

    # Remove Daily tag from previous daily's thread
    if daily_tag and thread_id and old_thread_id and old_thread_id != thread_id:
        try:
            old_thread = bot.get_channel(old_thread_id) or await bot.fetch_channel(old_thread_id)
            if isinstance(old_thread, discord.Thread):
                new_applied = [t for t in old_thread.applied_tags if t.id != daily_tag.id]
                await old_thread.edit(applied_tags=new_applied)
        except Exception as e:
            log_error(f"[DAILY TAG] Failed to remove tag from old thread {old_thread_id}: {e}")

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
        log_error("[PROBLEM] notification send failed:", repr(e))

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
            log_error("[DAILY] error:", repr(e))

        await asyncio.sleep(300)  # poll every 5 minutes


# ------------------------------------------------------------------ #
#  LeetCode Contest (weekly / biweekly)                               #
# ------------------------------------------------------------------ #

CONTEST_COLOR = 0xFFA116   # LeetCode orange
CONTEST_RECAP_COLOR = 0x9B59B6  # purple

CONTEST_CHANNEL_MAP: dict[str, int] = {
    "weekly": LEETCODE_WEEKLY_CHANNEL_ID,
    "biweekly": LEETCODE_BIWEEKLY_CHANNEL_ID,
}

CONTEST_FORUM_CHANNEL_MAP: dict[str, int] = {
    "weekly":   LEETCODE_WEEKLY_FORUM_CHANNEL_ID,
    "biweekly": LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID,
}


async def fetch_leetcode_contests(session: ClientSession) -> list[dict]:
    query = "{ topTwoContests { title titleSlug startTime duration originStartTime } }"
    async with session.post(
        f"{LEETCODE_BASE}/graphql",
        json={"query": query},
        headers={"Content-Type": "application/json"},
    ) as resp:
        js = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"LeetCode contest API failed: {resp.status} {js}")
        return (js.get("data") or {}).get("topTwoContests") or []


def _classify_contest(title_slug: str) -> str | None:
    if title_slug.startswith("weekly-contest"):
        return "weekly"
    if title_slug.startswith("biweekly-contest"):
        return "biweekly"
    return None


def _next_contest_deadline(contests: list[dict], contest_type: str, *, fallback_after: int) -> int:
    """Return timestamp 24h before the next contest of *contest_type*.
    Falls back to 7 days after *fallback_after* if no match in *contests*."""
    for c in contests:
        slug = c.get("titleSlug") or ""
        if _classify_contest(slug) == contest_type:
            start = c.get("startTime") or 0
            if start > fallback_after:
                return start - 86400
    return fallback_after + 7 * 86400


async def fetch_leetcode_csrf(session: ClientSession) -> str:
    async with session.get("https://leetcode.com", allow_redirects=True) as resp:
        csrf = resp.cookies.get("csrftoken")
        if csrf:
            return csrf.value
    cookie = session.cookie_jar.filter_cookies("https://leetcode.com").get("csrftoken")
    return cookie.value if cookie else ""


async def fetch_contest_questions(session: ClientSession, contest_slug: str) -> list[dict]:
    csrf = await fetch_leetcode_csrf(session)
    query = """
    query contestQuestions($slug: String!) {
        contestQuestionList(contestSlug: $slug) {
            questionId
            title
            titleSlug
            credit
        }
    }
    """
    headers = {
        "Content-Type": "application/json",
        "Referer": f"https://leetcode.com/contest/{contest_slug}/",
        "x-csrftoken": csrf,
    }
    payload = {"query": query.strip(), "variables": {"slug": contest_slug}}
    async with session.post("https://leetcode.com/graphql", json=payload, headers=headers) as resp:
        js = await resp.json(content_type=None)
        return (js.get("data") or {}).get("contestQuestionList") or []


def build_contest_recap_embed(
    contest: dict,
    questions: list[dict],
    question_thread_ids: dict[str, int] | None = None,
) -> discord.Embed:
    title = contest.get("title") or "LeetCode Contest"
    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0

    url = f"{LEETCODE_BASE}/contest/{slug}/" if slug else LEETCODE_BASE

    desc_lines: list[str] = []
    if start_ts:
        desc_lines.append(f"\U0001f5d3\ufe0f <t:{start_ts}:D>")

    if questions:
        desc_lines.append("")
        desc_lines.append("**Problems**")
        for i, q in enumerate(questions, 1):
            q_title = q.get("title") or f"Problem {i}"
            q_slug = q.get("titleSlug") or ""
            q_id = q.get("questionId") or i
            thread_id = (question_thread_ids or {}).get(q_slug)
            if thread_id:
                q_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
                desc_lines.append(f"[{q_id}. {q_title}]({q_url})")
            else:
                desc_lines.append(f"{q_id}. {q_title}")
    else:
        desc_lines.append("")
        desc_lines.append("*Problems not yet available*")

    return discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_lines),
        color=CONTEST_RECAP_COLOR,
    )


async def fetch_zerotrac_ratings(session: ClientSession) -> dict[str, float]:
    """Fetch problem ratings from zerotrac. Returns {TitleSlug: Rating} or {} on failure."""
    try:
        async with session.get(
            "https://raw.githubusercontent.com/zerotrac/leetcode_problem_rating/main/data.json"
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
            return {p["TitleSlug"]: p["Rating"] for p in data}
    except Exception as e:
        log_error(f"[ZEROTRAC] Failed to fetch ratings: {e}")
        return {}


_ZEROTRAC_CACHE_TTL = 7 * 24 * 3600  # 1 week


async def get_zerotrac_data(session: ClientSession) -> list[dict]:
    """Return full zerotrac data list, using DB cache if fresh (< 1 week old).

    Each entry: {title_slug, rating, contest_slug, problem_index}.
    Falls back to DB cache on fetch failure.
    """
    import time as _time
    age = _time.time() - zerotrac_cache_updated_at()
    if age < _ZEROTRAC_CACHE_TTL:
        cached = zerotrac_cache_get_all()
        if cached:
            return list(cached.values())

    try:
        async with session.get(
            "https://raw.githubusercontent.com/zerotrac/leetcode_problem_rating/main/data.json"
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            raw = await resp.json(content_type=None)
    except Exception as e:
        log_error(f"[ZEROTRAC] Fetch failed, using cache: {e}")
        cached = zerotrac_cache_get_all()
        return list(cached.values())

    entries = [
        {"title_slug": p["TitleSlug"], "rating": p["Rating"],
         "contest_slug": p["ContestSlug"], "problem_index": p["ProblemIndex"]}
        for p in raw
    ]
    # Bulk upsert of thousands of rows (+ commit/fsync) would otherwise block the
    # event loop; run it on a worker thread. Safe because connections are opened
    # with check_same_thread=False.
    await asyncio.to_thread(zerotrac_cache_upsert_all, entries)
    print(f"[ZEROTRAC] Cache refreshed ({len(entries)} entries)")
    return entries


def build_contest_forum_embed(
    contest: dict,
    questions: list[dict],
    ratings_by_slug: dict[str, float],
    question_thread_ids: dict[str, int] | None = None,
    *,
    fallback_contest_slug: str | None = None,
) -> discord.Embed:
    title = contest.get("title") or "LeetCode Contest"
    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0

    url = f"{LEETCODE_BASE}/contest/{slug}/" if slug else LEETCODE_BASE

    desc_lines: list[str] = []

    for i, q in enumerate(questions, 1):
        q_title = q.get("title") or f"Problem {i}"
        q_slug = q.get("titleSlug") or ""
        q_id = q.get("questionId") or i
        thread_id = (question_thread_ids or {}).get(q_slug)

        if thread_id:
            link_text = f"[{q_id}. {q_title}](https://discord.com/channels/{GUILD_ID}/{thread_id})"
        elif q_slug:
            if fallback_contest_slug:
                q_url = f"{LEETCODE_BASE}/contest/{fallback_contest_slug}/problems/{q_slug}/"
            else:
                q_url = f"{LEETCODE_BASE}/problems/{q_slug}/"
            link_text = f"[{q_id}. {q_title}]({q_url})"
        else:
            link_text = f"{q_id}. {q_title}"

        rating = ratings_by_slug.get(q_slug)
        if rating is not None:
            desc_lines.append(f"{link_text} ||⭐ {rating:.0f}||")
        else:
            desc_lines.append(link_text)

    return discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_lines) if desc_lines else None,
        color=CONTEST_RECAP_COLOR,
    )


def build_contest_notif_embed(contest: dict, forum_thread_url: str, *, show_countdown: bool = False) -> discord.Embed:
    title = contest.get("title") or "LeetCode Contest"
    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0

    url = f"{LEETCODE_BASE}/contest/{slug}/" if slug else LEETCODE_BASE

    desc_lines: list[str] = []
    if show_countdown and start_ts:
        desc_lines.append(f"\U0001f550 Starts <t:{start_ts}:R>")
    if forum_thread_url:
        desc_lines.append(f"\U0001f449 [View Post]({forum_thread_url})")

    return discord.Embed(
        title=title,
        url=url,
        description="\n\n".join(desc_lines) if desc_lines else None,
        color=CONTEST_RECAP_COLOR,
    )


async def _update_notif_embed(bot, contest_type: str, slug: str) -> None:
    """Update the pre-contest notification embed to reflect current contest phase."""
    try:
        post = leetcode_contest_post_get(slug)
        if not post or not post.get("notif_message_id"):
            return

        msg_id = post["notif_message_id"]
        start_ts = post.get("start_time") or 0
        end_ts = start_ts + 5400  # 90 min
        thread_id = post.get("thread_id")
        now = int(datetime.now().timestamp())

        if start_ts and now < start_ts:
            status_line = f"\U0001f550 Starts <t:{start_ts}:R>"
        elif start_ts and now < end_ts:
            status_line = f"\U0001f7e2 In progress \u2014 ends <t:{end_ts}:R>"
        else:
            status_line = "\u2705 Contest ended"

        title = slug.replace("-", " ").title()
        url = f"{LEETCODE_BASE}/contest/{slug}/"
        desc_lines = [status_line]
        if thread_id:
            forum_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            desc_lines.append(f"\U0001f449 [View Post]({forum_url})")

        embed = discord.Embed(
            title=title, url=url,
            description="\n\n".join(desc_lines),
            color=CONTEST_RECAP_COLOR,
        )

        channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
        if not channel_id:
            return
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(msg_id)
        await msg.edit(embed=embed)
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] notif embed update failed: {e}")


def _apply_frontend_ids(questions: list[dict]) -> None:
    """Replace GraphQL/zerotrac internal questionIds with the frontend display IDs stored in the DB."""
    for q in questions:
        db_prob = leetcode_get_problem_by_slug(q.get("titleSlug") or "")
        if db_prob and db_prob.get("question_id"):
            q["questionId"] = db_prob["question_id"]


def build_pre_contest_embed(contest: dict) -> discord.Embed:
    """Countdown embed posted 24h before the contest (before problems are available)."""
    title = contest.get("title") or "LeetCode Contest"
    slug = contest.get("titleSlug") or ""
    start_ts = contest.get("startTime") or 0

    url = f"{LEETCODE_BASE}/contest/{slug}/" if slug else LEETCODE_BASE

    desc_lines = ["Problems will be available when the contest starts."]

    return discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_lines),
        color=CONTEST_RECAP_COLOR,
    )


async def get_or_create_problem_post_archived(bot, slug: str) -> tuple[int | None, str]:
    """Like get_or_create_problem_post but immediately archives the thread after creation.

    Falls back to the LeetCode GraphQL API if the pied API doesn't have the problem yet
    (e.g. a brand-new contest problem before the contest ends).
    """
    # Check by slug first to avoid creating duplicate threads for existing problems
    existing = leetcode_get_problem_by_slug(slug)
    if existing:
        thread_id = existing["thread_id"]
        err = ""
    else:
        thread_id = None
        err = ""
        try:
            thread_id, err = await get_or_create_problem_post(bot, slug)
        except Exception as e:
            err = str(e)

        if thread_id is None:
            # pied doesn't have it yet — try GraphQL (handles live contest problems).
            # Lock on the slug (get_or_create_problem_post has already released its
            # lock by now, so this re-acquire is sequential, not nested).
            async with _post_lock(slug):
                existing = leetcode_get_problem_by_slug(slug)
                if existing:
                    thread_id = existing["thread_id"]
                else:
                    try:
                        data = await fetch_problem_by_slug_graphql(bot.http_session, slug)
                        thread_id, err = await _create_problem_forum_post(bot, data)
                    except Exception as e:
                        log_error(f"[GRAPHQL FALLBACK] Failed to create post for '{slug}': {e}")

    if thread_id:
        try:
            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(thread, discord.Thread) and not thread.archived:
                await thread.edit(archived=True)
        except Exception as e:
            log_error(f"[ARCHIVE] could not archive {thread_id}: {e}")
    return thread_id, err


def _format_finish_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def fetch_user_contest_history(session: ClientSession, username: str) -> dict:
    async with session.get(f"https://leetcode-api-pied.vercel.app/user/{username}/contests") as resp:
        if resp.status != 200:
            return {}
        return await resp.json()


async def _ratings_ready(session: ClientSession, contest_title: str) -> bool:
    """Returns True if at least one linked user has their rating for this contest."""
    users = linked_users_all()
    for user in users:
        try:
            data = await fetch_user_contest_history(session, user["leetcode_username"])
            history = [h for h in (data.get("userContestRankingHistory") or []) if h.get("attended")]
            if any(h.get("contest", {}).get("title") == contest_title for h in history):
                return True
        except Exception:
            continue
    return False


async def _build_contest_rankings(bot, contest_title: str) -> list[dict]:
    users = linked_users_all()
    rankings = []

    for user in users:
        discord_id = user["discord_user_id"]
        username = user["leetcode_username"]
        try:
            data = await fetch_user_contest_history(bot.http_session, username)
            history = [h for h in (data.get("userContestRankingHistory") or []) if h.get("attended")]

            entry = next((h for h in history if h.get("contest", {}).get("title") == contest_title), None)
            if not entry:
                continue

            # Compute delta from previous contest entry (sorted oldest→newest)
            sorted_history = sorted(history, key=lambda h: h.get("contest", {}).get("startTime", 0))
            idx = next((i for i, h in enumerate(sorted_history) if h.get("contest", {}).get("title") == contest_title), None)
            delta = None
            if idx is not None and idx > 0:
                prev_rating = sorted_history[idx - 1].get("rating")
                if entry.get("rating") is not None and prev_rating is not None:
                    delta = entry["rating"] - prev_rating

            guild = bot.get_guild(GUILD_ID)
            member = guild.get_member(discord_id) if guild else None
            discord_handle = member.display_name if member else username

            # Use .get with defaults so one malformed history entry degrades that
            # row gracefully instead of raising KeyError and dropping the whole
            # participant (including their rating) from the rankings.
            rankings.append({
                "discord_id": discord_id,
                "username": username,
                "discord_handle": discord_handle,
                "solved": entry.get("problemsSolved", 0),
                "total": entry.get("totalProblems", 0),
                "time": _format_finish_time(entry.get("finishTimeInSeconds", 0)),
                "rating": entry.get("rating", 0),
                "delta": delta,
            })
        except Exception as e:
            log_error(f"[RANKINGS] Error fetching {username}: {e}")

    rankings.sort(key=lambda r: r["rating"], reverse=True)
    return rankings


def build_rankings_embed(rankings: list[dict], *, title: str = "Rankings") -> discord.Embed:
    embed = discord.Embed(title=title, color=CONTEST_RECAP_COLOR)

    if not rankings:
        embed.description = "No linked users participated in this contest."
        return embed

    pings = "\n".join(
        f"{i}. <@{r['discord_id']}> · [View Profile](https://leetcode.com/{r['username']}/)"
        for i, r in enumerate(rankings, 1)
    )

    # Build fixed-width columns
    rows = []
    for i, r in enumerate(rankings, 1):
        delta_str = ""
        if r["delta"] is not None:
            sign = "+" if r["delta"] >= 0 else ""
            delta_str = f"{sign}{r['delta']:.0f}"
        rows.append((str(i), r["discord_handle"], f"{r['solved']}/{r['total']}", r["time"], f"{r['rating']:.0f}", delta_str))

    headers = ("Rank", "Username", "Solved", "Time", "Rating", "+/-")
    col_widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]

    def fmt_row(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, col_widths)).rstrip()

    table_lines = [fmt_row(headers), "─" * sum(col_widths + [2] * (len(col_widths) - 1))]
    table_lines.extend(fmt_row(row) for row in rows)

    embed.description = f"{pings}\n```\n" + "\n".join(table_lines) + "\n```"
    return embed


_DIFFICULTY_TAG_WEIGHTS = [3, 4, 5, 6]  # Q1 → Q4 fallback weights


def _contest_difficulty_tag(questions: list[dict], ratings_by_slug: dict[str, float]) -> str:
    """Compute a difficulty tag for a contest from zerotrac ratings.

    Uses the credit field (GraphQL) if available, else falls back to Q-index
    weights (3/4/5/6). Returns 'Easy' (<1750), 'Medium' (1750-1950),
    'Hard' (>=1950), or 'Rating Pending' if any problem is missing a rating.
    """
    if not questions:
        return "Rating Pending"
    weighted_sum = 0.0
    total_weight = 0
    for i, q in enumerate(questions):
        q_slug = q.get("titleSlug") or ""
        rating = ratings_by_slug.get(q_slug)
        if rating is None:
            return "Rating Pending"
        weight = int(q.get("credit") or _DIFFICULTY_TAG_WEIGHTS[min(i, 3)])
        weighted_sum += rating * weight
        total_weight += weight
    if total_weight == 0:
        return "Rating Pending"
    avg = weighted_sum / total_weight
    if avg < 1750:
        return "Easy"
    elif avg < 1950:
        return "Medium"
    return "Hard"


async def post_pre_contest(
    bot,
    contest_type: str,
    *,
    force: bool = False,
    contests: list[dict] | None = None,
) -> tuple[bool, str]:
    """Phase 0: create forum thread + send notif 24h before contest starts (typically Friday).

    Does NOT wait for contest start — fires as soon as we're within 24h of start_ts.
    Thread is created with a countdown embed; problems are filled in by post_contest_problems.
    """
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

        now = int(datetime.now().timestamp())
        if start_ts and now < start_ts - 86400:
            return False, f"{contest_type} starts <t:{start_ts}:R>, more than 24h away"

    forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type, 0)
    if not forum_channel_id:
        return False, f"no forum channel configured for {contest_type}"

    forum_channel = bot.get_channel(forum_channel_id) or await bot.fetch_channel(forum_channel_id)
    if not isinstance(forum_channel, discord.ForumChannel):
        return False, f"{contest_type} forum channel must be a forum channel"

    title = contest.get("title") or contest_type.title()
    forum_embed = build_pre_contest_embed(contest)

    forum_thread_id: int | None = None
    forum_thread_url: str = ""
    try:
        unrated_tag = await _get_or_create_forum_tag(forum_channel, "Rating Pending")
        result = await forum_channel.create_thread(
            name=title[:100],
            embed=forum_embed,
            applied_tags=[unrated_tag],
            reason=f"{contest_type.title()} contest post (pre-contest)",
        )
        forum_thread = result.thread if hasattr(result, "thread") else result
        forum_thread_id = forum_thread.id
        forum_thread_url = f"https://discord.com/channels/{GUILD_ID}/{forum_thread_id}"
        leetcode_contest_post_save(
            slug, contest_type, forum_thread_id,
            start_time=start_ts,
            rated=0,
        )
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] forum thread create failed: {e}")

    notif_embed = build_contest_notif_embed(contest, forum_thread_url, show_countdown=True)

    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no notif channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} notif channel must be a text channel"

    notif_msg = await channel.send(embed=notif_embed)

    if slug:
        leetcode_contest_post_set_notif_message_id(slug, notif_msg.id)

    leetcode_set_contest_state(contest_type, slug, thread_id=forum_thread_id)
    return True, f"posted pre-contest {contest_type} slug={slug}"


async def post_contest_problems(
    bot,
    contest_type: str,
) -> tuple[bool, str]:
    """Phase 1: poll for contest problems after start and update the forum thread.

    Polls every loop iteration (5 min) from contest start until problems are available
    or 2h have elapsed (timeout → mark unavailable, zerotrac fills links later).
    """
    if not bot.http_session:
        return False, "http session not ready"

    slug = leetcode_get_contest_state(contest_type)
    if not slug:
        return False, f"{contest_type} no contest posted yet"

    post = leetcode_contest_post_get(slug)
    if not post:
        return False, f"{contest_type} no post record for slug={slug}"

    if post.get("problems_posted"):
        return False, f"{contest_type} problems already posted for slug={slug}"

    start_ts = post.get("start_time") or 0
    now = int(datetime.now().timestamp())

    if start_ts and now < start_ts:
        return False, f"{contest_type} contest hasn't started yet, starts <t:{start_ts}:R>"

    forum_thread_id = post["thread_id"]

    # 2h timeout: give up if problems still not available
    if start_ts and now > start_ts + 7200:
        try:
            unavailable_embed = discord.Embed(
                title=slug.replace("-", " ").title(),
                url=f"{LEETCODE_BASE}/contest/{slug}/",
                description="Problems unavailable \u2014 ratings will appear here once published.",
                color=CONTEST_RECAP_COLOR,
            )
            thread = bot.get_channel(forum_thread_id) or await bot.fetch_channel(forum_thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    starter = await thread.fetch_message(forum_thread_id)
                    await starter.edit(embed=unavailable_embed)
                except Exception:
                    async for msg in thread.history(limit=1, oldest_first=True):
                        await msg.edit(embed=unavailable_embed)
                        break
        except Exception as e:
            log_error(f"[CONTEST/{contest_type.upper()}] failed to update thread with unavailable msg: {e}")
        leetcode_contest_post_set_problems_posted(slug, 0)
        return True, f"{contest_type} 2h timeout — problems unavailable for slug={slug}"

    # Fetch questions
    questions: list[dict] = []
    try:
        questions = await fetch_contest_questions(bot.http_session, slug)
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] question fetch failed: {e}")

    if not questions:
        return False, f"{contest_type} problems not available yet for slug={slug}"

    # Create individual problem posts
    question_thread_ids: dict[str, int] = {}
    for q in questions:
        q_slug = q.get("titleSlug") or ""
        if not q_slug:
            continue
        try:
            thread_id_q, err = await get_or_create_problem_post_archived(bot, q_slug)
            if thread_id_q:
                question_thread_ids[q_slug] = thread_id_q
            elif err:
                log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}': {err}")
        except Exception as e:
            log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}' failed: {e}")

    # Correct questionIds: GraphQL returns internal IDs; use frontend IDs from DB
    _apply_frontend_ids(questions)

    # Update thread embed with problem links
    try:
        mock_contest = {"title": slug.replace("-", " ").title(), "titleSlug": slug, "startTime": start_ts}
        new_embed = build_contest_forum_embed(
            mock_contest, questions, {}, question_thread_ids,
            fallback_contest_slug=slug,
        )
        thread = bot.get_channel(forum_thread_id) or await bot.fetch_channel(forum_thread_id)
        if isinstance(thread, discord.Thread):
            try:
                starter = await thread.fetch_message(forum_thread_id)
                await starter.edit(embed=new_embed)
            except Exception:
                async for msg in thread.history(limit=1, oldest_first=True):
                    await msg.edit(embed=new_embed)
                    break
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] failed to update thread with problems: {e}")

    leetcode_contest_post_set_problems_posted(slug, now)
    await _update_notif_embed(bot, contest_type, slug)
    return True, f"{contest_type} problems posted for slug={slug}"


async def post_contest_rankings(
    bot,
    contest_type: str,
    *,
    force: bool = False,
    slug_override: str | None = None,
    contests: list[dict] | None = None,
) -> tuple[bool, str]:
    """Phase 2: create problem posts, update contest thread embed, post rankings.

    Driven entirely from DB state — does not depend on the contests API, which
    rolls over to the next contest as soon as the current one ends.

    slug_override: explicitly target a contest slug (e.g. "weekly-contest-490"),
    bypassing the DB state lookup. Useful when the state has moved on.
    """
    if not bot.http_session:
        return False, "http session not ready"

    if slug_override:
        slug = slug_override
    else:
        # Phase 1 must be done — get slug from DB state
        slug = leetcode_get_contest_state(contest_type)
        if not slug:
            return False, f"{contest_type} no contest posted yet (phase 1 pending)"

    post = leetcode_contest_post_get(slug)
    # post may be None if phase 1 was never run for this slug (e.g. manually specified)

    start_time = (post.get("start_time") if post else None) or 0
    end_ts = start_time + 5400  # all LeetCode contests are 90 min
    title = slug.replace("-", " ").title()  # "Weekly Contest 490"

    if not force:
        if not post:
            return False, f"{contest_type} no post record for slug={slug} (use force=True or run /weekly first)"

        now = int(datetime.now().timestamp())
        if start_time and now < end_ts:
            return False, f"{contest_type} hasn't ended yet, ends <t:{end_ts}:R>"

        if post.get("rankings_posted"):
            return False, f"{contest_type} rankings already posted for slug={slug}"

        # Give up 24h before the next contest of the same type
        deadline_ts = _next_contest_deadline(contests or [], contest_type, fallback_after=end_ts)
        if now > deadline_ts:
            leetcode_contest_post_set_rankings_posted(slug)
            return True, f"{contest_type} deadline passed, no rankings for slug={slug}"

        if linked_users_all():
            if not await _ratings_ready(bot.http_session, title):
                return False, f"{contest_type} ratings not yet available, will retry"

    mock_contest = {"title": title, "titleSlug": slug, "startTime": start_time}

    # Create individual problem posts now that the contest is over
    questions: list[dict] = []
    try:
        questions = await fetch_contest_questions(bot.http_session, slug)
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] question fetch failed: {e}")

    question_thread_ids: dict[str, int] = {}
    for q in questions:
        q_slug = q.get("titleSlug") or ""
        if not q_slug:
            continue
        try:
            thread_id_q, err = await get_or_create_problem_post_archived(bot, q_slug)
            if thread_id_q:
                question_thread_ids[q_slug] = thread_id_q
            elif err:
                log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}': {err}")
        except Exception as e:
            log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}' failed: {e}")

    # Correct questionIds: GraphQL returns internal IDs; use frontend IDs from DB
    _apply_frontend_ids(questions)

    # Update the contest thread embed with Discord problem links (ratings added by phase 3)
    if questions and post:
        forum_thread_id = post["thread_id"]
        try:
            new_embed = build_contest_forum_embed(
                mock_contest, questions, {}, question_thread_ids,
                fallback_contest_slug=slug,
            )
            thread = bot.get_channel(forum_thread_id) or await bot.fetch_channel(forum_thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    starter = await thread.fetch_message(forum_thread_id)
                    await starter.edit(embed=new_embed)
                except Exception:
                    async for msg in thread.history(limit=1, oldest_first=True):
                        await msg.edit(embed=new_embed)
                        break
        except Exception as e:
            log_error(f"[CONTEST/{contest_type.upper()}] failed to update thread embed: {e}")

    rankings: list[dict] = []
    try:
        rankings = await _build_contest_rankings(bot, title)
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] rankings fetch failed: {e}")

    if post:
        leetcode_contest_post_set_rankings_posted(slug)
        await _update_notif_embed(bot, contest_type, slug)

    if not rankings:
        return True, f"no participants for {contest_type} slug={slug}, skipping rankings post"

    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no notif channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} notif channel must be a text channel"

    await channel.send(embed=build_rankings_embed(rankings, title=f"{title} Rankings"))
    return True, f"posted rankings for {contest_type} slug={slug}"


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
    duration = contest.get("duration") or 5400
    end_ts = start_ts + duration

    if not force:
        last_slug = leetcode_get_contest_state(contest_type)
        if last_slug == slug:
            return False, f"already posted {contest_type} slug={slug}"

        # Only post after contest ends
        now = int(datetime.now().timestamp())
        if start_ts and now < end_ts:
            return False, f"{contest_type} ends <t:{end_ts}:R>, too early to post recap"

        # Wait for at least one linked user's rating to be available
        # (LeetCode processes ratings 30-60 min after contest ends)
        # After 4 hours, post regardless in case no linked users participated
        if linked_users_all() and now < end_ts + 20 * 60:
            if not await _ratings_ready(bot.http_session, contest.get("title") or ""):
                return False, f"{contest_type} ratings not yet available, will retry"

    # Fetch contest questions via GraphQL
    questions: list[dict] = []
    try:
        questions = await fetch_contest_questions(bot.http_session, slug)
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] question fetch failed: {e}")

    # Fetch zerotrac ratings for spoiler display
    ratings_by_slug = await fetch_zerotrac_ratings(bot.http_session)

    # Create/archive a problem forum post for each contest question
    question_thread_ids: dict[str, int] = {}
    for q in questions:
        q_slug = q.get("titleSlug") or ""
        if not q_slug:
            continue
        try:
            thread_id, err = await get_or_create_problem_post_archived(bot, q_slug)
            if thread_id:
                question_thread_ids[q_slug] = thread_id
            elif err:
                log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}': {err}")
        except Exception as e:
            log_error(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}' failed: {e}")

    # Correct questionIds: GraphQL returns internal IDs; use frontend IDs from DB
    _apply_frontend_ids(questions)

    # Create the contest thread in the dedicated forum channel
    forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type, 0)
    if not forum_channel_id:
        return False, f"no forum channel configured for {contest_type}"

    forum_channel = bot.get_channel(forum_channel_id) or await bot.fetch_channel(forum_channel_id)
    if not isinstance(forum_channel, discord.ForumChannel):
        return False, f"{contest_type} forum channel must be a forum channel"

    title = contest.get("title") or contest_type.title()
    forum_embed = build_contest_forum_embed(contest, questions, ratings_by_slug, question_thread_ids)

    tag_name = _contest_difficulty_tag(questions, ratings_by_slug)
    is_rated = tag_name != "Rating Pending"

    forum_thread_id: int | None = None
    forum_thread_url: str = ""
    try:
        contest_tag = await _get_or_create_forum_tag(forum_channel, tag_name)
        result = await forum_channel.create_thread(
            name=title[:100],
            embed=forum_embed,
            applied_tags=[contest_tag],
            reason=f"{contest_type.title()} contest post",
        )
        forum_thread = result.thread if hasattr(result, "thread") else result
        forum_thread_id = forum_thread.id
        forum_thread_url = f"https://discord.com/channels/{GUILD_ID}/{forum_thread_id}"
        leetcode_contest_post_save(
            slug, contest_type, forum_thread_id,
            start_time=start_ts,
            rated=1 if is_rated else 0,
        )
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] forum thread create failed: {e}")

    # Build simplified notif embed linking to the forum post
    notif_embed = build_contest_notif_embed(contest, forum_thread_url)

    # Fetch rankings
    rankings_embed: discord.Embed | None = None
    try:
        rankings = await _build_contest_rankings(bot, title)
        if rankings:
            rankings_embed = build_rankings_embed(rankings, title=f"{title} Rankings")
    except Exception as e:
        log_error(f"[CONTEST/{contest_type.upper()}] rankings fetch failed: {e}")

    # Send notif + rankings to the text notification channel (no thread)
    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no notif channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} notif channel must be a text channel"

    embeds = [notif_embed, rankings_embed] if rankings_embed else [notif_embed]
    await channel.send(embeds=embeds)

    leetcode_set_contest_state(contest_type, slug, thread_id=forum_thread_id)
    # Mark rankings as done so phase 2 doesn't re-post them
    leetcode_contest_post_set_rankings_posted(slug)
    return True, f"posted {contest_type} slug={slug}"


async def fetch_weekly_premium(session: ClientSession) -> dict | None:
    """Return the current week's premium weekly problem, or None if unavailable."""
    csrf = await fetch_leetcode_csrf(session)
    today = datetime.utcnow().date()
    today_str = str(today)

    query = """
    query($year: Int!, $month: Int!) {
      dailyCodingChallengeList(year: $year, month: $month) {
        weeklyQuestions {
          questionFrontendId
          questionTitle
          questionTitleSlug
          date
        }
      }
    }
    """
    headers = {
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com/",
        "x-csrftoken": csrf,
    }

    # Check current month and previous month to handle month boundaries
    months_to_check = [(today.year, today.month)]
    if today.month == 1:
        months_to_check.append((today.year - 1, 12))
    else:
        months_to_check.append((today.year, today.month - 1))

    candidates = []
    for year, month in months_to_check:
        payload = {"query": query.strip(), "variables": {"year": year, "month": month}}
        try:
            async with session.post("https://leetcode.com/graphql", json=payload, headers=headers) as resp:
                js = await resp.json(content_type=None)
                challenge_list = (js.get("data") or {}).get("dailyCodingChallengeList") or []
                weekly = challenge_list[0].get("weeklyQuestions") or [] if challenge_list else []
                for entry in weekly:
                    d = entry.get("date") or ""
                    if d and d <= today_str:
                        candidates.append(entry)
        except Exception as e:
            log_error(f"[PREMIUM WEEKLY] fetch error for {year}-{month:02d}: {e}")

    if not candidates:
        return None
    return max(candidates, key=lambda e: e.get("date") or "")


async def post_leetcode_weekly_premium(bot, *, force: bool = False) -> tuple[bool, str]:
    if not bot.http_session:
        return False, "http session not ready"

    entry = await fetch_weekly_premium(bot.http_session)
    if not entry:
        return False, "no weekly premium problem found"

    date = entry.get("date") or ""
    title_slug = entry.get("questionTitleSlug") or ""
    qtitle = entry.get("questionTitle") or "Weekly Premium"
    qid = entry.get("questionFrontendId") or ""

    if not title_slug:
        return False, "weekly premium entry missing titleSlug"

    # Capture old thread for tag swap
    old_state = leetcode_get_premium_weekly_state()
    old_thread_id: int | None = None
    if old_state and old_state.get("title_slug") and old_state["title_slug"] != title_slug:
        old_problem = leetcode_get_problem_by_slug(old_state["title_slug"])
        if old_problem:
            old_thread_id = old_problem["thread_id"]

    if not force:
        if old_state and old_state["date"] and date <= old_state["date"]:
            return False, f"already posted for week of {old_state['date']}, skipping {date}"

    forum = bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)

    weekly_premium_tag: discord.ForumTag | None = None
    try:
        weekly_premium_tag = await _get_or_create_forum_tag(forum, "Weekly Premium")
    except Exception as e:
        log_error(f"[PREMIUM WEEKLY] Could not get/create tag: {e}")

    # Ensure a forum post exists for this problem
    thread_id: int | None = None
    existing = leetcode_get_problem_by_slug(title_slug)
    if existing:
        thread_id = existing["thread_id"]
    else:
        try:
            thread_id, err = await get_or_create_problem_post(bot, title_slug)
            if not thread_id:
                log_error(f"[PREMIUM WEEKLY] forum post failed: {err}")
        except Exception as e:
            log_error(f"[PREMIUM WEEKLY] forum post error: {e}")

    # Apply "Weekly Premium" tag to new thread and archive it (premium content)
    if weekly_premium_tag and thread_id:
        try:
            new_thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(new_thread, discord.Thread):
                new_tags = list(new_thread.applied_tags)
                if not any(t.id == weekly_premium_tag.id for t in new_tags):
                    new_tags.append(weekly_premium_tag)
                await new_thread.edit(applied_tags=new_tags, archived=True)
        except Exception as e:
            log_error(f"[PREMIUM WEEKLY] Failed to apply tag to thread {thread_id}: {e}")
    elif thread_id:
        try:
            new_thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if isinstance(new_thread, discord.Thread) and not new_thread.archived:
                await new_thread.edit(archived=True)
        except Exception as e:
            log_error(f"[PREMIUM WEEKLY] Failed to archive thread {thread_id}: {e}")

    # Remove "Weekly Premium" tag from previous week's thread
    if weekly_premium_tag and old_thread_id and old_thread_id != thread_id:
        try:
            old_thread = bot.get_channel(old_thread_id) or await bot.fetch_channel(old_thread_id)
            if isinstance(old_thread, discord.Thread):
                new_applied = [t for t in old_thread.applied_tags if t.id != weekly_premium_tag.id]
                await old_thread.edit(applied_tags=new_applied)
        except Exception as e:
            log_error(f"[PREMIUM WEEKLY] Failed to remove tag from old thread {old_thread_id}: {e}")

    # Look up stored problem for difficulty
    problem = leetcode_get_problem_by_slug(title_slug)
    difficulty = (problem.get("difficulty") or "Unknown") if problem else "Unknown"
    stored_qid = problem["question_id"] if problem else (qid or title_slug)

    # Convert date string to unix timestamp for Discord formatting
    date_ts = 0
    if date:
        try:
            date_ts = int(datetime.strptime(date, "%Y-%m-%d").timestamp())
        except ValueError:
            pass

    # Notification card
    try:
        notif_channel = bot.get_channel(LEETCODE_PREMIUM_WEEKLY_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PREMIUM_WEEKLY_CHANNEL_ID)

        diff_emoji = DIFF_EMOJI.get(difficulty, "\u26aa")
        color = DIFF_COLORS.get(difficulty, 0x808080)
        url = f"{LEETCODE_BASE}/problems/{title_slug}/"
        notif_title = f"{qid}. {qtitle}" if qid else qtitle

        desc_lines = [f"{diff_emoji} **{difficulty}** · \U0001f512 Premium"]
        if date_ts:
            desc_lines.append("")
            desc_lines.append(f"\U0001f4c5 Week of <t:{date_ts}:D>")
        if thread_id:
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            desc_lines.append("")
            desc_lines.append(f"\U0001f449 [View Post]({thread_url})")

        notif_embed = discord.Embed(
            title=notif_title,
            url=url,
            description="\n".join(desc_lines),
            color=color,
        )
        await notif_channel.send(embed=notif_embed)
    except Exception as e:
        log_error(f"[PREMIUM WEEKLY] notification send failed: {e}")

    leetcode_set_premium_weekly_state(question_id=stored_qid, title_slug=title_slug, date=date)
    return True, f"posted premium weekly {date=} {title_slug=}"


async def leetcode_premium_weekly_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(4)
    print("\u2705 LeetCode premium weekly scheduler started")
    while not bot.is_closed():
        # Only start polling when ~1 week has passed since the last post
        state = leetcode_get_premium_weekly_state()
        if state and state.get("date"):
            try:
                last_date = datetime.strptime(state["date"], "%Y-%m-%d").date()
                days_since = (datetime.utcnow().date() - last_date).days
                if days_since < 6:
                    await asyncio.sleep(21600)  # 6 hours — not due yet
                    continue
            except ValueError:
                pass

        try:
            posted, msg = await post_leetcode_weekly_premium(bot, force=False)
            if posted:
                print(f"[PREMIUM WEEKLY] {msg}")
        except Exception as e:
            log_error("[PREMIUM WEEKLY] error:", repr(e))
        await asyncio.sleep(3600)  # poll hourly once due


async def check_and_update_contest_ratings(bot) -> int:
    """Check unrated contest posts and update embed + tag once zerotrac has ratings.

    Returns the number of contests updated.
    """
    unrated = leetcode_contest_posts_get_unrated()
    if not unrated:
        return 0

    zerotrac_entries = await get_zerotrac_data(bot.http_session)
    if not zerotrac_entries:
        print("[RATINGS UPDATE] No zerotrac data available")
        return 0

    zerotrac_data = [{"TitleSlug": e["title_slug"], "Rating": e["rating"],
                      "ContestSlug": e["contest_slug"], "ProblemIndex": e["problem_index"]}
                     for e in zerotrac_entries]
    ratings_by_slug: dict[str, float] = {p["TitleSlug"]: p["Rating"] for p in zerotrac_data}

    from collections import defaultdict
    zerotrac_by_contest: dict[str, list[dict]] = defaultdict(list)
    for p in zerotrac_data:
        zerotrac_by_contest[p["ContestSlug"]].append(p)

    updated = 0
    for row in unrated:
        contest_slug = row["contest_slug"]
        contest_type = row["contest_type"]
        thread_id = row["thread_id"]
        start_time = row["start_time"]

        problems = zerotrac_by_contest.get(contest_slug)
        if not problems:
            continue  # Too old for zerotrac or not yet indexed

        problems = sorted(problems, key=lambda p: p["ProblemIndex"])
        questions = [
            {"questionId": "", "title": p["TitleSlug"].replace("-", " ").title(), "titleSlug": p["TitleSlug"]}
            for p in problems
        ]

        tag_name = _contest_difficulty_tag(questions, ratings_by_slug)
        if tag_name == "Rating Pending":
            continue  # Ratings still not out

        try:
            forum_channel_id = CONTEST_FORUM_CHANNEL_MAP.get(contest_type, 0)
            if not forum_channel_id:
                continue
            forum_channel = bot.get_channel(forum_channel_id) or await bot.fetch_channel(forum_channel_id)
            if not isinstance(forum_channel, discord.ForumChannel):
                continue

            thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                continue

            # Rebuild embed with ratings now filled in
            # Also create any missing problem posts (recovers from phase 1 failure)
            question_thread_ids: dict[str, int] = {}
            for q in questions:
                existing = leetcode_get_problem_by_slug(q["titleSlug"])
                if existing:
                    question_thread_ids[q["titleSlug"]] = existing["thread_id"]
                else:
                    try:
                        thread_id_q, err = await get_or_create_problem_post_archived(bot, q["titleSlug"])
                        if thread_id_q:
                            question_thread_ids[q["titleSlug"]] = thread_id_q
                        elif err:
                            log_error(f"[RATINGS UPDATE] problem post '{q['titleSlug']}': {err}")
                    except Exception as e:
                        log_error(f"[RATINGS UPDATE] problem post '{q['titleSlug']}' failed: {e}")

            # Correct questionIds: zerotrac/GraphQL use internal IDs; use frontend IDs from DB
            _apply_frontend_ids(questions)

            contest_id_en = problems[0].get("ContestID_en", contest_slug.replace("-", " ").title())
            mock_contest = {"title": contest_id_en, "titleSlug": contest_slug, "startTime": start_time}
            new_embed = build_contest_forum_embed(mock_contest, questions, ratings_by_slug, question_thread_ids)

            # Edit the starter message (forum post starter message ID == thread ID)
            try:
                starter = await thread.fetch_message(thread_id)
                await starter.edit(embed=new_embed)
            except Exception:
                # Fallback: walk history
                async for msg in thread.history(limit=1, oldest_first=True):
                    await msg.edit(embed=new_embed)
                    break

            # Swap "Rating Pending" tag for difficulty tag
            difficulty_tag = await _get_or_create_forum_tag(forum_channel, tag_name)
            new_tags = [t for t in thread.applied_tags if t.name != "Rating Pending"] + [difficulty_tag]
            await thread.edit(applied_tags=new_tags)

            leetcode_contest_post_set_rated(contest_slug)
            updated += 1
            print(f"[RATINGS UPDATE] {contest_slug} → {tag_name} (avg computed)")
        except Exception as e:
            log_error(f"[RATINGS UPDATE] Failed to update {contest_slug}: {e}")

    return updated


async def leetcode_contest_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    print("\u2705 LeetCode contest scheduler started")
    while not bot.is_closed():
        try:
            contests = await fetch_leetcode_contests(bot.http_session)

            # Phase 0: pre-contest thread (24h before start, i.e. Friday)
            for ctype in ("weekly", "biweekly"):
                try:
                    posted, msg = await post_pre_contest(
                        bot, ctype, force=False, contests=contests,
                    )
                    if posted:
                        print(f"[CONTEST/{ctype.upper()}] {msg}")
                except Exception as e:
                    log_error(f"[CONTEST/{ctype.upper()}] pre-contest post error:", repr(e))

            # Phase 1: update thread with problems (polls after contest starts)
            for ctype in ("weekly", "biweekly"):
                try:
                    posted, msg = await post_contest_problems(bot, ctype)
                    if posted:
                        print(f"[CONTEST/{ctype.upper()}] {msg}")
                except Exception as e:
                    log_error(f"[CONTEST/{ctype.upper()}] problems post error:", repr(e))

            # Phase 2: rankings (contest end → next contest's pre-contest window).
            # Iterate every contest with problems posted but rankings still pending,
            # so a contest doesn't get abandoned when the next one of the same type
            # transitions contest_state forward.
            now_ts = int(datetime.now().timestamp())
            for pending in leetcode_contest_posts_get_pending_rankings():
                slug = pending["contest_slug"]
                ctype = pending["contest_type"]
                start_time = pending.get("start_time") or 0
                # Skip contests that haven't ended yet (90 min duration)
                if start_time and now_ts < start_time + 5400:
                    continue
                try:
                    posted, msg = await post_contest_rankings(
                        bot, ctype, force=False, slug_override=slug, contests=contests,
                    )
                    if posted:
                        print(f"[CONTEST/{ctype.upper()}] {msg}")
                    else:
                        print(f"[CONTEST/{ctype.upper()}] rankings skip slug={slug}: {msg}")
                except Exception as e:
                    log_error(f"[CONTEST/{ctype.upper()}] rankings post error slug={slug}:", repr(e))

            # Keep notif embeds in sync (e.g. transition "In progress" → "Contest ended")
            for ctype in ("weekly", "biweekly"):
                slug = leetcode_get_contest_state(ctype)
                if slug:
                    try:
                        await _update_notif_embed(bot, ctype, slug)
                    except Exception:
                        pass

            # Phase 3: zerotrac updates for all unrated posts
            try:
                n = await check_and_update_contest_ratings(bot)
                if n:
                    print(f"[RATINGS UPDATE] Updated {n} contest(s) with ratings")
            except Exception as e:
                log_error("[RATINGS UPDATE] error:", repr(e))

            # Sleep logic
            now = int(datetime.now().timestamp())
            any_polling_problems = False
            any_unrated = bool(leetcode_contest_posts_get_unrated())
            pending_rankings = leetcode_contest_posts_get_pending_rankings()
            any_pending_rankings = any(
                (p.get("start_time") or 0) and now >= (p["start_time"] + 5400)
                for p in pending_rankings
            )
            next_wake_times: list[int] = []

            for ctype in ("weekly", "biweekly"):
                slug = leetcode_get_contest_state(ctype)
                post = leetcode_contest_post_get(slug) if slug else None
                start_time = (post or {}).get("start_time") or 0
                end_ts = start_time + 5400

                if post and not post["problems_posted"]:
                    if start_time and now >= start_time:
                        any_polling_problems = True       # actively polling for problems
                    elif start_time > now:
                        next_wake_times.append(start_time)  # wake at contest start

            # Wake at contest end for any contest with pending rankings that hasn't ended yet
            for p in pending_rankings:
                p_start = p.get("start_time") or 0
                if p_start and now < p_start + 5400:
                    next_wake_times.append(p_start + 5400)

            # Pre-contest: wake 24h before upcoming contest if not yet posted
            for c in contests:
                slug = c.get("titleSlug") or ""
                ctype = _classify_contest(slug)
                if ctype and leetcode_get_contest_state(ctype) != slug:
                    pre_ts = (c.get("startTime") or 0) - 86400
                    if pre_ts > now:
                        next_wake_times.append(pre_ts)

            if any_polling_problems:
                sleep_time = 300
            elif any_pending_rankings or any_unrated:
                sleep_time = 86400
            elif next_wake_times:
                sleep_time = min(next_wake_times) - now
            else:
                sleep_time = 6 * 60 * 60

            # Also respect scheduled wake times even during active polling
            if next_wake_times:
                sleep_time = min(sleep_time, min(next_wake_times) - now)

            print(f"[CONTEST] sleeping {max(sleep_time, 60)}s")
            await asyncio.sleep(max(sleep_time, 60))

        except Exception as e:
            log_error("[CONTEST] error:", repr(e))
            await asyncio.sleep(60 * 60)  # retry in 1 hour on error
