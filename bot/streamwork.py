"""Mark a problem post as one that was solved live: the Twitch tag, and a line
linking into the VOD at the moment it was started.

The Twitch bot knows which problems were worked on, because the owner names
each one in chat (`!st <url>`), and it knows how far into the stream that was.
Once the VOD exists it sends them here. Twitch has no API for VOD markers, but
a link carrying `?t=` jumps to a time, which does the same job.

A VOD doesn't last forever -- Twitch deletes it after its retention window --
and a dead link is worse than no link, so a daily sweep asks Twitch whether
each VOD is still there and, when one has gone, takes the tag and the line
back off. What's left is an ordinary problem post, the same as one solved in
the co-working call.

Marks are kept because a post often doesn't exist yet when its problem is
solved: the solve sweep creates it later. An unapplied mark is retried by the
same daily pass.
"""

import asyncio
import re
import time

import discord

from .config import LEETCODE_PROBLEMS_CHANNEL_ID, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
from .database import (leetcode_get_problem_by_slug, stream_mark_delete, stream_mark_save,
                       stream_marks_all, stream_marks_set_applied)
from .logbus import log_error
from .problemsites import SITES

TAG_NAME = "Twitch"
SWEEP_INTERVAL = 6 * 60 * 60        # a VOD's disappearance is never urgent
# Opens the line this module owns, so it can be found and removed again
# without touching anything else in the post.
LINE_PREFIX = "▶"
_LINE_RE = re.compile(rf"^{re.escape(LINE_PREFIX)} .*$", re.M)

_LEETCODE_RE = re.compile(r"leetcode\.com/problems/([\w-]+)", re.I)
_CODEFORCES_RE = re.compile(r"codeforces\.com/(?:problemset/problem|contest)/(\d+)/(?:problem/)?(\w+)", re.I)


def parse_problem(url: str) -> tuple[str, str] | None:
    """A problem url -> (platform, ref), or None if it isn't one we post."""
    m = _LEETCODE_RE.search(url)
    if m:
        return "leetcode", m[1].lower()
    m = _CODEFORCES_RE.search(url)
    if m:
        return "codeforces", f"{m[1]}{m[2].upper()}"
    for key, site in SITES.items():
        m = site.url_re.search(url)
        if m:
            return key, m[1]
    return None


def find_thread_id(platform: str, ref: str) -> int | None:
    """The forum post for a problem, if one has been created yet."""
    from .database import _db
    if platform == "leetcode":
        row = leetcode_get_problem_by_slug(ref)
        return row["thread_id"] if row else None
    table = {"codeforces": "codeforces_problems", "cses": "cses_problems",
             "euler": "euler_problems"}.get(platform)
    if not table:
        return None
    with _db() as conn:
        row = conn.execute(f"SELECT thread_id FROM {table} WHERE ref=?", (ref,)).fetchone()
    return row[0] if row else None


def vod_line(vod_id: str, offset_s: int, when: int) -> str:
    h, rem = divmod(max(0, offset_s), 3600)
    m, s = divmod(rem, 60)
    stamp = time.strftime("%-d %b %Y", time.localtime(when))
    return (f"{LINE_PREFIX} Solved on stream, {stamp} — "
            f"[watch from {h}:{m:02d}](https://www.twitch.tv/videos/{vod_id}?t={h}h{m:02d}m{s:02d}s)")


async def _edit_post(bot, thread_id: int, *, line: str | None) -> bool:
    """Add or remove this module's line and the Twitch tag on one post.

    These posts are archived, so every edit unarchives and re-archives; an
    archived thread refuses edits to its messages.
    """
    thread = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
    if not isinstance(thread, discord.Thread):
        return False
    forum = thread.parent
    tag = next((t for t in (forum.available_tags if forum else []) if t.name == TAG_NAME), None)
    was_archived = thread.archived
    if was_archived:
        await thread.edit(archived=False)
    try:
        starter = thread.starter_message or await thread.fetch_message(thread.id)
        content = _LINE_RE.sub("", starter.content or "").strip()
        if line:
            content = f"{content}\n{line}".strip()
        if (starter.content or "").strip() != content:
            await starter.edit(content=content or None)
        if tag:
            applied = [t for t in thread.applied_tags if t.id != tag.id]
            if line:
                applied.append(tag)
            if {t.id for t in applied} != {t.id for t in thread.applied_tags}:
                await thread.edit(applied_tags=applied[:5])
    finally:
        if was_archived:
            await thread.edit(archived=True)
    return True


async def record(bot, vod_id: str, items: list[dict]) -> int:
    """Store what was solved on this stream, and mark whatever already has a
    post. `items`: [{"url": ..., "offset_s": ..., "at": epoch}]"""
    marked = 0
    for item in items:
        parsed = parse_problem(str(item.get("url") or ""))
        if not parsed:
            continue
        platform, ref = parsed
        offset_s, at = int(item.get("offset_s") or 0), int(item.get("at") or time.time())
        stream_mark_save(platform, ref, vod_id, offset_s, at)
        if await _apply(bot, platform, ref, vod_id, offset_s, at):
            marked += 1
    return marked


async def _apply(bot, platform: str, ref: str, vod_id: str, offset_s: int, at: int) -> bool:
    thread_id = find_thread_id(platform, ref)
    if not thread_id:
        return False        # no post yet; the sweep will try again
    try:
        if await _edit_post(bot, thread_id, line=vod_line(vod_id, offset_s, at)):
            stream_marks_set_applied(platform, ref, vod_id, thread_id)
            return True
    except Exception as e:
        log_error(f"[STREAMWORK] tagging {platform}/{ref} failed: {e!r}")
    return False


async def _vod_alive(session, vod_ids: list[str]) -> set[str]:
    """Which of these VODs Twitch still has. An unanswerable question (no
    credentials, API down) returns them all: never remove on a maybe."""
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        return set(vod_ids)
    try:
        async with session.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials"}) as r:
            token = (await r.json()).get("access_token")
        if not token:
            return set(vod_ids)
        alive: set[str] = set()
        headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
        for i in range(0, len(vod_ids), 100):
            params = [("id", v) for v in vod_ids[i:i + 100]]
            async with session.get("https://api.twitch.tv/helix/videos", params=params,
                                   headers=headers) as r:
                if r.status != 200:
                    return set(vod_ids)
                alive |= {v["id"] for v in (await r.json()).get("data", [])}
        return alive
    except Exception as e:
        log_error(f"[STREAMWORK] VOD check failed: {e!r}")
        return set(vod_ids)


async def sweep(bot) -> None:
    """Apply marks whose post has since been created, and undo the ones whose
    VOD Twitch no longer has."""
    marks = stream_marks_all()
    if not marks:
        return
    alive = await _vod_alive(bot.http_session, sorted({m["vod_id"] for m in marks}))
    for m in marks:
        if m["vod_id"] in alive:
            if not m["thread_id"]:
                await _apply(bot, m["platform"], m["ref"], m["vod_id"], m["offset_s"], m["marked_at"])
            continue
        # The VOD has gone: the post goes back to looking like any other.
        if m["thread_id"]:
            try:
                await _edit_post(bot, m["thread_id"], line=None)
            except discord.NotFound:
                pass        # post deleted since: nothing to clean up, drop the mark
            except Exception as e:
                log_error(f"[STREAMWORK] untagging {m['platform']}/{m['ref']} failed: {e!r}")
                continue
        stream_mark_delete(m["platform"], m["ref"], m["vod_id"])


async def sweep_loop(bot) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await sweep(bot)
        except Exception as e:
            log_error(f"[STREAMWORK] sweep failed: {e!r}")
        await asyncio.sleep(SWEEP_INTERVAL)
