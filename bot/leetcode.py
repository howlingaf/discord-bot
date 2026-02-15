import asyncio
import html
import re
from datetime import datetime

import discord
from aiohttp import ClientSession

from .config import (
    LEETCODE_DAILY_URL,
    LEETCODE_BASE,
    LEETCODE_DAILY_POLL_SECONDS,
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_DAILY_NOTIF_CHANNEL_ID,
    MAX_EXAMPLES,
    LEETCODE_WEEKLY_CHANNEL_ID,
    LEETCODE_BIWEEKLY_CHANNEL_ID,
    LEETCODE_CONTEST_URL,
    LEETCODE_CONTEST_POLL_SECONDS,
    LEETCODE_CONTEST_MAX_ACTIVE_THREADS,
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


# ---------------- Thread cap helper ----------------
async def enforce_active_thread_cap(
    channel: discord.TextChannel,
    *,
    limit: int = 5,
    lock: bool = True,
    reason: str = "Thread cap enforcement",
):
    threads: list[discord.Thread] = []

    try:
        threads = list(await channel.active_threads())
    except Exception:
        threads = list(getattr(channel, "threads", []))

    active = [
        t for t in threads
        if isinstance(t, discord.Thread) and not t.archived
    ]

    if len(active) <= limit:
        return

    active.sort(key=lambda t: t.created_at or discord.utils.snowflake_time(t.id))
    to_close = active[: max(0, len(active) - limit)]

    for t in to_close:
        try:
            await t.edit(archived=True, locked=lock, reason=reason)
        except discord.Forbidden:
            print(f"[THREAD CAP] Forbidden archiving thread {t.id} ({t.name})")
        except discord.HTTPException as e:
            print(f"[THREAD CAP] HTTPException archiving thread {t.id} ({t.name}): {e}")


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


def build_daily_embeds(daily: dict) -> list[discord.Embed]:
    link = daily.get("link") or ""
    q = daily.get("question") or {}
    qid = q.get("questionFrontendId") or q.get("questionId") or ""
    title = f"{qid}. {q.get('title') or 'LeetCode Daily'}" if qid else (q.get("title") or "LeetCode Daily")
    difficulty = q.get("difficulty") or "Unknown"
    url = f"{LEETCODE_BASE}{link}" if link.startswith("/") else (link or LEETCODE_BASE)

    content_html = q.get("content") or ""

    examples = extract_pre_blocks(content_html)[:max(0, MAX_EXAMPLES)]

    statement_html = re.sub(r"<pre[\s\S]*?</pre>", "", content_html, flags=re.IGNORECASE)
    plain = html_to_text_preserve_newlines(statement_html)
    statement, constraints = split_statement_and_constraints(plain)

    statement = re.sub(r"Example\s+\d+:\s*", "", statement).strip()

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

    # Check if we already sent the notification for today's daily
    if not force:
        state = leetcode_get_daily_state()
        if state and state["date"] == date_ts:
            return False, f"already posted for date={date}"

    # --- Forum post (look up DB, then create if needed) ---
    forum = bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
    if not isinstance(forum, discord.ForumChannel):
        return False, "LEETCODE_PROBLEMS_CHANNEL_ID must be a forum channel"

    thread_name = f"{qid}. {qtitle}".strip(". ")[:100] if qid else qtitle[:100]

    existing = leetcode_get_problem(qid)
    thread_id = existing["thread_id"] if existing else None

    if thread_id is None:
        embeds = build_daily_embeds(daily)
        try:
            result = await forum.create_thread(
                name=thread_name,
                embeds=embeds,
                reason="Daily LeetCode discussion post",
            )
            thread = result.thread if hasattr(result, "thread") else result
            thread_id = thread.id
            leetcode_save_problem(
                question_id=qid,
                title_slug=title_slug,
                title=qtitle,
                thread_id=thread_id,
            )
        except Exception as e:
            print("[PROBLEM] forum post create failed:", repr(e))

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

        # Problem statement
        content_html = q.get("content") or ""
        statement_html = re.sub(r"<pre[\s\S]*?</pre>", "", content_html, flags=re.IGNORECASE)
        plain = html_to_text_preserve_newlines(statement_html)
        statement, _ = split_statement_and_constraints(plain)
        statement = re.sub(r"Example\s+\d+:\s*", "", statement).strip()
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


async def leetcode_daily_poller(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(3)
    print(f"\u2705 LeetCode daily poller started (every {LEETCODE_DAILY_POLL_SECONDS}s)")
    while not bot.is_closed():
        try:
            posted, msg = await post_leetcode_problem(bot, force=False)
            print(f"[DAILY] posted={posted} {msg}")
        except Exception as e:
            print("[DAILY] error:", repr(e))

        await asyncio.sleep(max(60, LEETCODE_DAILY_POLL_SECONDS))


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
        desc_lines.append(f"\U0001f5d3\ufe0f **Start:** <t:{start_ts}:F> (<t:{start_ts}:R>)")
    desc_lines.append(f"\u23f1\ufe0f **Duration:** {dur_min} minutes")

    embed = discord.Embed(
        title=title,
        url=url,
        description="\n".join(desc_lines),
        color=CONTEST_COLOR,
    )
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

    if not force:
        last_slug = leetcode_get_contest_state(contest_type)
        if last_slug == slug:
            return False, f"already posted {contest_type} slug={slug}"

    channel_id = CONTEST_CHANNEL_MAP.get(contest_type, 0)
    if not channel_id:
        return False, f"no channel configured for {contest_type}"

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False, f"{contest_type} channel must be a text channel"

    embed = build_contest_embed(contest)
    sent = await channel.send(embed=embed)

    title = contest.get("title") or contest_type.title()
    thread_name = title[:100]
    try:
        await channel.create_thread(
            name=thread_name,
            message=sent,
            auto_archive_duration=1440,
            reason=f"{contest_type.title()} contest discussion thread",
        )
        await enforce_active_thread_cap(
            channel,
            limit=LEETCODE_CONTEST_MAX_ACTIVE_THREADS,
            lock=True,
            reason=f"Keep only {LEETCODE_CONTEST_MAX_ACTIVE_THREADS} active {contest_type} threads",
        )
    except Exception as e:
        print(f"[CONTEST/{contest_type.upper()}] thread create failed:", repr(e))

    leetcode_set_contest_state(contest_type, slug)
    return True, f"posted {contest_type} slug={slug}"


async def leetcode_contest_poller(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    print(f"\u2705 LeetCode contest poller started (every {LEETCODE_CONTEST_POLL_SECONDS}s)")
    while not bot.is_closed():
        try:
            contests = await fetch_leetcode_contests(bot.http_session)
            for ctype in ("weekly", "biweekly"):
                try:
                    posted, msg = await post_leetcode_contest(
                        bot, ctype, force=False, contests=contests,
                    )
                    print(f"[CONTEST/{ctype.upper()}] posted={posted} {msg}")
                except Exception as e:
                    print(f"[CONTEST/{ctype.upper()}] error:", repr(e))
        except Exception as e:
            print("[CONTEST] fetch error:", repr(e))

        await asyncio.sleep(max(60, LEETCODE_CONTEST_POLL_SECONDS))
