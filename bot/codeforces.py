"""Codeforces problem posts.

A Codeforces link shared on stream becomes a forum post in #problems, the same
way a LeetCode problem does — so the recap can point at a thread people can
discuss in rather than sending them straight off to another site.

Design contract:
  * Identity is contestId + index ("1421A"), never the URL: /problemset/problem/
    1421/A and /contest/1421/problem/A are the same problem, and keying on the
    link would post it twice.
  * Metadata comes from the official problemset API, mirrored into
    codeforces_cache and refreshed weekly. Codeforces publishes its own
    difficulty rating (800-3500) for ~97% of problems — only the newest
    contests lack one, and they gain it a few days later.
  * No statement is fetched. Codeforces has no API for problem text, so the card
    is a reference: name, rating, contest, tags, link.
  * EDU course and gym problems are not supported. Neither appears in the
    problemset API nor in contest.standings, and their pages 403 without an
    enrolment, so a post for one could only ever be a bare reference.
"""

import asyncio
import re
import time

import discord
from aiohttp import ClientSession, ClientTimeout

from .config import LEETCODE_PROBLEMS_CHANNEL_ID
from .database import (
    codeforces_cache_get,
    codeforces_cache_updated_at,
    codeforces_cache_upsert_all,
    codeforces_problem_get,
    codeforces_problem_save,
)
from .logbus import log_error

BASE = "https://codeforces.com"
_PROBLEMSET_API = f"{BASE}/api/problemset.problems"
_CONTEST_LIST_API = f"{BASE}/api/contest.list"
_CACHE_TTL = 7 * 24 * 3600
# A cache miss only forces an early rebuild if the mirror is at least this
# old — otherwise an unknown ref (gym, typo) re-downloads 11k rows each time.
_MISS_REFRESH_AFTER = 3600
_TIMEOUT = ClientTimeout(total=30)
# Codeforces 403s a default aiohttp user agent.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Both public URL shapes plus a bare "1421A".
_URL_RE = re.compile(
    r"/problemset/problem/(\d+)/(\w+)|/contest/(\d+)/problem/(\w+)", re.I)
# EDU lesson practice and gym contests: unsupported. An EDU url contains a
# /contest/<id>/problem/<x> segment, so it has to be rejected before matching.
_UNSUPPORTED_RE = re.compile(r"/(?:edu|gym)/", re.I)
_BARE_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]\d?)\s*$")

# Rating bands set to Codeforces' own quartiles over the rated problemset
# (p25=1300, p75=2400, n=11006): green is the easiest quarter, yellow the middle
# half, red the hardest quarter. Eyeballed thresholds put ~80% of problems in
# the top two bands, which told you almost nothing.
_BANDS = ((1300, "🟢", 0x00B8A3), (2400, "🟡", 0xFFC01E), (10_000, "🔴", 0xFF375F))

_lock = asyncio.Lock()


def parse_ref(text: str) -> str | None:
    """'1421A' from a public problem URL or a bare reference, else None."""
    if not text or _UNSUPPORTED_RE.search(text):
        return None
    bare = _BARE_RE.match(text)
    if bare:
        return f"{bare.group(1)}{bare.group(2).upper()}"
    m = _URL_RE.search(text)
    if not m:
        return None
    pairs = [(m.group(1), m.group(2)), (m.group(3), m.group(4))]
    for contest, index in pairs:
        if contest:
            return f"{contest}{index.upper()}"
    return None


def problem_url(ref: str) -> str:
    m = re.match(r"^(\d+)([A-Za-z]\d?)$", ref)
    if not m:
        return f"{BASE}/problemset"
    return f"{BASE}/problemset/problem/{m.group(1)}/{m.group(2)}"


def _band(rating: int | None) -> tuple[str, int]:
    """(emoji, embed colour) for a rating — grey when Codeforces has none yet."""
    if not rating:
        return "⚪", 0x808080
    for ceiling, emoji, colour in _BANDS:
        if rating < ceiling:
            return emoji, colour
    return "🔴", 0xFF375F


async def refresh_cache(session: ClientSession) -> int:
    """Mirror the problemset (+ contest names) into the DB. Returns rows written."""
    headers = {"User-Agent": _UA}
    try:
        async with session.get(_PROBLEMSET_API, headers=headers, timeout=_TIMEOUT) as r:
            if r.status != 200:
                raise ValueError(f"problemset HTTP {r.status}")
            problems = (await r.json(content_type=None))["result"]["problems"]
        async with session.get(_CONTEST_LIST_API, headers=headers, timeout=_TIMEOUT) as r:
            contests = {c["id"]: c.get("name", "")
                        for c in (await r.json(content_type=None))["result"]} \
                if r.status == 200 else {}
    except Exception as e:
        log_error(f"[CODEFORCES] problemset refresh failed: {e!r}")
        return 0

    entries = [{
        "ref": f"{p['contestId']}{p['index']}",
        "contest_id": p["contestId"],
        "index": p["index"],
        "name": p.get("name") or "",
        "rating": p.get("rating"),
        "tags": p.get("tags") or [],
        "contest_name": contests.get(p["contestId"], ""),
    } for p in problems if p.get("contestId") and p.get("index")]

    # ~11k rows with a commit — off the event loop, like the zerotrac refresh.
    await asyncio.to_thread(codeforces_cache_upsert_all, entries)
    print(f"[CODEFORCES] cache refreshed ({len(entries)} problems)")
    return len(entries)


async def problem_meta(session: ClientSession, ref: str) -> dict | None:
    """Cached metadata for a ref, refreshing the mirror when it's stale.

    A miss also refreshes — a problem newer than the mirror wouldn't be in it —
    but only if the mirror hasn't just been rebuilt, or every gym link (gym
    problems are never in the problemset API) would re-download all 11k rows.
    """
    cached = codeforces_cache_get(ref)
    age = time.time() - codeforces_cache_updated_at()
    if cached and age <= _CACHE_TTL:
        return cached
    if age > _CACHE_TTL or (not cached and age > _MISS_REFRESH_AFTER):
        await refresh_cache(session)
    return codeforces_cache_get(ref)


def build_embed(ref: str, meta: dict | None, source_url: str = "") -> discord.Embed:
    """Reference card: name, rating, contest, tags, link.

    `source_url` is the link the problem was shared with. It becomes the card's
    link when the API doesn't know the problem — an EDU or gym problem has no
    working /problemset/ URL, so synthesising one would point at a 403.
    """
    name = (meta or {}).get("name") or ""
    rating = (meta or {}).get("rating")
    emoji, colour = _band(rating)

    title = f"{ref}. {name}" if name else ref
    header = f"{emoji} **{rating}**" if rating else f"{emoji} **Unrated**"
    # Blank line under the rating so it reads as a header, matching the gap the
    # LeetCode card puts between its difficulty line and the statement.
    parts = [header, ""]
    if (meta or {}).get("contest_name"):
        parts.append(f"-# {meta['contest_name']}")
    if (meta or {}).get("tags"):
        parts.append(" · ".join(f"`{t}`" for t in meta["tags"]))

    link = problem_url(ref) if meta else (source_url or problem_url(ref))
    if not meta:
        parts.append("-# not in the public problemset — no name or rating available")
    return discord.Embed(title=title[:256], url=link,
                         description="\n".join(parts), color=colour)


async def get_or_create_problem_post(bot, text: str) -> tuple[int | None, str]:
    """Look up or create the forum post for a Codeforces problem.

    `text` may be a problem URL or a bare "1421A". Returns (thread_id, error).
    """
    ref = parse_ref(text)
    if not ref:
        return None, "not a public Codeforces problem (EDU and gym aren't supported)"
    if not bot.http_session:
        return None, "http session not ready"

    existing = codeforces_problem_get(ref)
    if existing:
        return existing["thread_id"], ""

    async with _lock:
        existing = codeforces_problem_get(ref)
        if existing:
            return existing["thread_id"], ""

        meta = await problem_meta(bot.http_session, ref)
        if not meta:
            # Nothing public backs this ref, so a card would be a dead link.
            return None, f"{ref} isn't in the public problemset"
        source_url = text.strip().strip("<>")
        forum = (bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
                 or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID))
        if not isinstance(forum, discord.ForumChannel):
            return None, "problems channel is not a forum channel"

        name = (meta or {}).get("name") or ""
        thread_name = (f"{ref}. {name}" if name else ref)[:100]
        tags = [t for t in forum.available_tags if t.name == "Codeforces"]

        try:
            result = await forum.create_thread(
                name=thread_name,
                embed=build_embed(ref, meta, source_url),
                applied_tags=tags,
                reason=f"Codeforces problem post for {ref}",
            )
        except Exception as e:
            log_error(f"[CODEFORCES] forum post failed for {ref}: {e!r}")
            return None, f"could not create the post: {e}"

        thread = result.thread if hasattr(result, "thread") else result
        codeforces_problem_save(ref, name, (meta or {}).get("rating"), thread.id,
                                source_url if not meta else problem_url(ref))
        return thread.id, ""
