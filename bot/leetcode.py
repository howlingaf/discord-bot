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
    leetcode_get_problem_by_slug,
    leetcode_save_problem,
    leetcode_get_daily_state,
    leetcode_set_daily_state,
    leetcode_get_contest_state,
    leetcode_set_contest_state,
    linked_users_all,
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

CONTEST_COLOR = 0xFFA116   # LeetCode orange
CONTEST_RECAP_COLOR = 0x9B59B6  # purple

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
                desc_lines.append(f"{q_id}. [{q_title}]({q_url})")
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
                delta = entry["rating"] - sorted_history[idx - 1]["rating"]

            rankings.append({
                "discord_id": discord_id,
                "solved": entry["problemsSolved"],
                "total": entry["totalProblems"],
                "time": _format_finish_time(entry["finishTimeInSeconds"]),
                "rating": entry["rating"],
                "delta": delta,
            })
        except Exception as e:
            print(f"[RANKINGS] Error fetching {username}: {e}")

    # TEST DATA — remove after confirming layout
    rankings.extend([
        {"discord_id": 111111111111111111, "solved": 4, "total": 4, "time": "0:44:51", "rating": 1923.0, "delta": 87.0},
        {"discord_id": 222222222222222222, "solved": 3, "total": 4, "time": "1:12:05", "rating": 1710.0, "delta": -15.0},
        {"discord_id": 333333333333333333, "solved": 2, "total": 4, "time": "1:29:44", "rating": 1634.0, "delta": 8.0},
        {"discord_id": 444444444444444444, "solved": 1, "total": 4, "time": "55:20",   "rating": 1401.0, "delta": -41.0},
    ])
    # END TEST DATA

    rankings.sort(key=lambda r: r["rating"], reverse=True)
    return rankings


def build_rankings_embed(rankings: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="Rankings", color=CONTEST_RECAP_COLOR)

    if not rankings:
        embed.description = "No linked users participated in this contest."
        return embed

    # Mentions in description — pings everyone and lets them match their row number
    embed.description = "\n".join(f"{i}. <@{r['discord_id']}>" for i, r in enumerate(rankings, 1))

    rank_lines, solved_lines, rating_lines = [], [], []
    for i, r in enumerate(rankings, 1):
        rank_lines.append(str(i))
        solved_lines.append(f"{r['solved']}/{r['total']} ({r['time']})")

        rating = f"{r['rating']:.0f}"
        if r["delta"] is not None:
            sign = "+" if r["delta"] >= 0 else ""
            rating += f" ({sign}{r['delta']:.0f})"
        rating_lines.append(rating)

    embed.add_field(name="#", value="\n".join(rank_lines), inline=True)
    embed.add_field(name="Solved", value="\n".join(solved_lines), inline=True)
    embed.add_field(name="Rating", value="\n".join(rating_lines), inline=True)
    return embed


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

    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} channel must be a text channel"

    # Fetch contest questions via GraphQL
    questions: list[dict] = []
    try:
        questions = await fetch_contest_questions(bot.http_session, slug)
    except Exception as e:
        print(f"[CONTEST/{contest_type.upper()}] question fetch failed: {e}")

    # Create forum posts for each contest question and collect thread IDs
    question_thread_ids: dict[str, int] = {}
    for q in questions:
        q_slug = q.get("titleSlug") or ""
        if not q_slug:
            continue
        # Check DB by slug first to avoid redundant API calls
        existing = leetcode_get_problem_by_slug(q_slug)
        if existing:
            question_thread_ids[q_slug] = existing["thread_id"]
            continue
        # Use titleSlug as the identifier — the API accepts slugs and is more
        # reliable than the questionId field returned by GraphQL
        try:
            thread_id, err = await get_or_create_problem_post(bot, q_slug)
            if thread_id:
                question_thread_ids[q_slug] = thread_id
            elif err:
                print(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}': {err}")
        except Exception as e:
            print(f"[CONTEST/{contest_type.upper()}] forum post '{q_slug}' failed: {e}")

    # Fetch rankings before sending so both embeds go in one message
    title = contest.get("title") or contest_type.title()
    rankings_embed: discord.Embed | None = None
    try:
        rankings = await _build_contest_rankings(bot, title)
        rankings_embed = build_rankings_embed(rankings)
    except Exception as e:
        print(f"[CONTEST/{contest_type.upper()}] rankings fetch failed: {e}")

    recap_embed = build_contest_recap_embed(contest, questions, question_thread_ids)
    embeds = [recap_embed, rankings_embed] if rankings_embed else [recap_embed]
    sent = await channel.send(embeds=embeds)

    # Create thread on the message — starts empty
    recap_thread_id: int | None = None
    try:
        thread = await channel.create_thread(
            name=title[:100],
            message=sent,
            auto_archive_duration=10080,
            reason=f"{contest_type.title()} contest recap thread",
        )
        recap_thread_id = thread.id
    except Exception as e:
        print(f"[CONTEST/{contest_type.upper()}] thread create failed: {e}")

    leetcode_set_contest_state(contest_type, slug, thread_id=recap_thread_id)
    return True, f"posted {contest_type} slug={slug}"


async def leetcode_contest_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    print("\u2705 LeetCode contest scheduler started")
    while not bot.is_closed():
        try:
            contests = await fetch_leetcode_contests(bot.http_session)

            # Post recap for any contest that has ended and not yet recapped
            for ctype in ("weekly", "biweekly"):
                try:
                    posted, msg = await post_leetcode_contest(
                        bot, ctype, force=False, contests=contests,
                    )
                    if posted:
                        print(f"[CONTEST/{ctype.upper()}] {msg}")
                except Exception as e:
                    print(f"[CONTEST/{ctype.upper()}] error:", repr(e))

            # Sleep until the next contest ends
            now = int(datetime.now().timestamp())
            next_end_times = []
            for c in contests:
                start_ts = c.get("startTime") or 0
                end_ts = start_ts + (c.get("duration") or 5400)
                if end_ts > now:
                    next_end_times.append(end_ts)

            if next_end_times:
                wait = min(next_end_times) - now
                print(f"[CONTEST] sleeping {wait}s until next contest ends")
                await asyncio.sleep(max(wait, 60))
            else:
                print("[CONTEST] no upcoming contests, rechecking in 6h")
                await asyncio.sleep(6 * 60 * 60)

        except Exception as e:
            print("[CONTEST] error:", repr(e))
            await asyncio.sleep(60 * 60)  # retry in 1 hour on error
