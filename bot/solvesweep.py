"""The 05:00 sweep: every problem solved overnight gets a post and a comment.

Runs Tue-Sat, looks back SOLVE_SWEEP_WINDOW_HOURS, and for each problem solved
in that window creates (or finds) its forum post and replies with the solution
link. A window with nothing in it posts nothing at all — no "nothing today"
message.

Per-platform notes, because the three differ in what they'll tell us:
  * LeetCode and Codeforces both expose a real submission timestamp, so their
    window is literally the last 12 hours.
  * CSES publishes no timestamp — a problem is solved or it isn't — so its
    "new" set is the diff against the stored snapshot instead. That is also
    why the first run seeds the snapshot and posts no CSES problems: every
    problem ever solved would otherwise land at once. It makes the CSES half
    immune to the site's display timezone, which is not the server's.
  * Only Codeforces submission links are public. LeetCode and CSES show a
    submission to its author alone, so those links work for the streamer and
    nobody else. They're posted anyway — the recap has always done the same
    for LeetCode — but that's what they are.
"""

import asyncio
import re
from datetime import datetime, timedelta

import discord

from .codeforces import get_or_create_problem_post as cf_get_or_create_post
from .config import (
    CODEFORCES_HANDLE,
    CSES_NICK,
    CSES_PASS,
    GUILD_ID,
    LEETCODE_BASE,
    LEETCODE_SUBMISSIONS_URL,
    SOLVE_SWEEP_DAYS,
    SOLVE_SWEEP_HOUR,
    SOLVE_SWEEP_WINDOW_HOURS,
    STREAMER_DISCORD_ID,
    STREAMER_NAME,
)
from .database import (
    cses_solved_add,
    cses_solved_all,
    cses_solved_seeded,
    solve_post_exists,
    solve_post_save,
)
from .leetcode import get_or_create_problem_post_from_ref as lc_get_or_create_post
from .logbus import log_error
from .problemsites import get_or_create_problem_post as site_get_or_create_post

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_CF_STATUS = "https://codeforces.com/api/user.status?handle={handle}&from=1&count=200"
_CSES = "https://cses.fi"
_CSES_ROW_RE = re.compile(
    r'<a href="/problemset/task/(\d+)"[^>]*>(.*?)</a>.*?'
    r'<span class="task-score icon ([a-z]*)"', re.S)
_CSES_RESULT_RE = re.compile(r'href="/problemset/result/(\d+)/?"')


def _solver_name() -> str:
    return f"<@{STREAMER_DISCORD_ID}>" if STREAMER_DISCORD_ID else f"**{STREAMER_NAME}**"


# ------------------------------------------------------------------ #
#  Per-platform solve lists                                          #
# ------------------------------------------------------------------ #

async def leetcode_solves(session, start: int, end: int) -> list[dict]:
    """Accepted LeetCode submissions in the window, one per problem (earliest
    accepted — the submission that actually solved it)."""
    async with session.get(LEETCODE_SUBMISSIONS_URL) as r:
        if r.status != 200:
            log_error(f"[SWEEP] LeetCode submissions HTTP {r.status}")
            return []
        data = await r.json(content_type=None)
    subs = data if isinstance(data, list) else data.get("submissions") or []
    out: dict[str, dict] = {}
    for s in sorted(subs, key=lambda x: int(x.get("timestamp") or 0)):
        ts = int(s.get("timestamp") or 0)
        if not (start <= ts <= end) or (s.get("statusDisplay") or "") != "Accepted":
            continue
        slug = s.get("titleSlug") or ""
        if slug and slug not in out:
            out[slug] = {"ref": slug, "title": s.get("title") or slug,
                         "sub_id": str(s.get("id") or ""), "ts": ts}
    return list(out.values())


async def codeforces_solves(session, start: int, end: int) -> list[dict]:
    """Accepted Codeforces submissions in the window, one per problem."""
    url = _CF_STATUS.format(handle=CODEFORCES_HANDLE)
    async with session.get(url, headers={"User-Agent": _UA}) as r:
        if r.status != 200:
            log_error(f"[SWEEP] Codeforces user.status HTTP {r.status}")
            return []
        data = await r.json(content_type=None)
    if data.get("status") != "OK":
        log_error(f"[SWEEP] Codeforces user.status: {data.get('comment')}")
        return []
    out: dict[str, dict] = {}
    for s in sorted(data.get("result") or [], key=lambda x: x["creationTimeSeconds"]):
        ts = s["creationTimeSeconds"]
        if not (start <= ts <= end) or s.get("verdict") != "OK":
            continue
        p = s.get("problem") or {}
        ref = f"{p.get('contestId')}{p.get('index')}"
        # The submission link needs the contest the submission was made in,
        # which is not always the contest the problem belongs to.
        if ref not in out:
            out[ref] = {"ref": ref, "title": p.get("name") or ref,
                        "sub_id": str(s.get("id") or ""),
                        "contest_id": s.get("contestId") or p.get("contestId"), "ts": ts}
    return list(out.values())


async def _cses_login(session) -> bool:
    async with session.get(f"{_CSES}/login", headers={"User-Agent": _UA}) as r:
        html = await r.text()
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not m:
        log_error("[SWEEP] CSES login form had no csrf token")
        return False
    async with session.post(f"{_CSES}/login", headers={"User-Agent": _UA},
                            data={"csrf_token": m.group(1),
                                  "nick": CSES_NICK, "pass": CSES_PASS}) as r:
        landed = str(r.url)
    if landed.rstrip("/").endswith("/login"):
        log_error("[SWEEP] CSES login rejected — check CSES_NICK / CSES_PASS")
        return False
    return True


async def cses_solves(session) -> list[dict]:
    """CSES problems solved since the last sweep, by snapshot diff.

    The first run seeds and returns nothing: with no snapshot there is no way
    to tell a fresh solve from one made a year ago.
    """
    if not (CSES_NICK and CSES_PASS):
        return []
    if not await _cses_login(session):
        return []

    async with session.get(f"{_CSES}/problemset/", headers={"User-Agent": _UA}) as r:
        page = await r.text()
    solved = {ref: re.sub(r"<[^>]+>", "", name).strip()
              for ref, name, state in _CSES_ROW_RE.findall(page) if state.strip() == "full"}
    if not solved:
        return []

    seeded = cses_solved_seeded()
    known = cses_solved_all()
    fresh = [r for r in solved if r not in known]
    cses_solved_add(list(solved), int(datetime.now().timestamp()))
    if not seeded:
        print(f"[SWEEP] CSES snapshot seeded with {len(solved)} solved problem(s)")
        return []

    out = []
    for ref in fresh:
        # Newest submission on the task page is the one that solved it.
        async with session.get(f"{_CSES}/problemset/view/{ref}/",
                               headers={"User-Agent": _UA}) as r:
            body = await r.text() if r.status == 200 else ""
        ids = _CSES_RESULT_RE.findall(body)
        out.append({"ref": ref, "title": solved[ref],
                    "sub_id": ids[0] if ids else "", "ts": 0})
    return out


# ------------------------------------------------------------------ #
#  Posting                                                           #
# ------------------------------------------------------------------ #

def _submission_url(platform: str, item: dict) -> str:
    if platform == "leetcode" and item["sub_id"]:
        return f"{LEETCODE_BASE}/problems/{item['ref']}/submissions/{item['sub_id']}/"
    if platform == "codeforces" and item["sub_id"]:
        return f"https://codeforces.com/contest/{item['contest_id']}/submission/{item['sub_id']}"
    if platform == "cses" and item["sub_id"]:
        return f"{_CSES}/problemset/result/{item['sub_id']}/"
    return ""


async def _post_one(bot, platform: str, item: dict) -> str | None:
    """Create/find the post and comment on it. Returns a summary line, or None."""
    if solve_post_exists(platform, item["ref"], item["sub_id"]):
        return None

    if platform == "leetcode":
        # Handed over as a url, not the bare slug: the slug parser requires a
        # hyphen so a mistyped word can't pose as one, which would reject the
        # single-word slugs ("subsets", "triangle") the api legitimately returns.
        thread_id, err = await lc_get_or_create_post(
            bot, f"{LEETCODE_BASE}/problems/{item['ref']}/")
    elif platform == "codeforces":
        thread_id, err = await cf_get_or_create_post(bot, item["ref"])
    else:
        thread_id, err = await site_get_or_create_post(bot, platform, item["ref"])
    if not thread_id:
        log_error(f"[SWEEP] {platform} {item['ref']}: {err}")
        return None

    thread = bot.get_channel(thread_id)
    if not thread:
        try:
            thread = await bot.fetch_channel(thread_id)
        except Exception as e:
            log_error(f"[SWEEP] could not fetch thread {thread_id}: {e!r}")
            return None

    # <> around the url so the comment doesn't drag an embed in behind it,
    # matching the recap's solution replies.
    line = f"{_solver_name()} submitted a solution!"
    url = _submission_url(platform, item)
    if url:
        line += f"\n<{url}>"
    try:
        await thread.send(line, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        log_error(f"[SWEEP] comment failed on {thread_id}: {e!r}")
        return None

    solve_post_save(platform, item["ref"], item["sub_id"], thread_id)
    return f"{platform} {item['ref']} — {item['title']} → {thread_id}"


async def run_sweep(bot, *, window_hours: int | None = None) -> str:
    """One pass. Returns a human summary; posts nothing when nothing was solved."""
    if not bot.http_session:
        return "http session not ready"
    session = bot.http_session
    end = int(datetime.now().timestamp())
    start = end - (window_hours or SOLVE_SWEEP_WINDOW_HOURS) * 3600

    found: list[tuple[str, dict]] = []
    for platform, coro in (("leetcode", leetcode_solves(session, start, end)),
                           ("codeforces", codeforces_solves(session, start, end))):
        try:
            found += [(platform, i) for i in await coro]
        except Exception as e:
            log_error(f"[SWEEP] {platform} lookup failed: {e!r}")
    try:
        found += [("cses", i) for i in await cses_solves(session)]
    except Exception as e:
        log_error(f"[SWEEP] cses lookup failed: {e!r}")

    if not found:
        return "nothing solved in the window"

    posted = []
    for platform, item in found:
        line = await _post_one(bot, platform, item)
        if line:
            posted.append(line)
    if not posted:
        return f"{len(found)} solved, all already posted"
    return f"posted {len(posted)}:\n" + "\n".join(posted)


# ------------------------------------------------------------------ #
#  Schedule                                                          #
# ------------------------------------------------------------------ #

def _next_run(now: datetime) -> datetime:
    """Next SOLVE_SWEEP_HOUR:00 falling on an enabled weekday, strictly ahead."""
    cand = now.replace(hour=SOLVE_SWEEP_HOUR, minute=0, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    while cand.weekday() not in SOLVE_SWEEP_DAYS:
        cand += timedelta(days=1)
    return cand


async def solve_sweep_scheduler(bot):
    await bot.wait_until_ready()
    days = ",".join(str(d) for d in sorted(SOLVE_SWEEP_DAYS))
    print(f"✅ Solve sweep scheduler started ({SOLVE_SWEEP_HOUR:02d}:00, weekdays {days}, "
          f"{SOLVE_SWEEP_WINDOW_HOURS}h window)")
    while not bot.is_closed():
        now = datetime.now()
        nxt = _next_run(now)
        await asyncio.sleep(max(1, (nxt - now).total_seconds()))
        try:
            print(f"[SWEEP] {await run_sweep(bot)}")
        except Exception as e:
            log_error(f"[SWEEP] run failed: {e!r}")
        # Past the hour before recomputing, so a fast run can't fire twice.
        await asyncio.sleep(90)
