"""Problem posts for CSES and Project Euler.

Both sites are handled here rather than in a module each: they differ only in
their URL shape and where the page keeps the title, and neither has enough
behaviour of its own to earn a file. LeetCode and Codeforces stay separate
because they carry real metadata — statements, ratings, difficulty bands.

Design contract:
  * Identity is the problem number within its own site, so each gets its own
    table (see SITE_PROBLEM_TABLES): CSES task 1068 and Euler problem 1068 are
    unrelated problems that would collide in a shared one.
  * Neither site has an API. The name is scraped from the problem page, the
    same lookup the recap already does for these links, so a card is a
    reference: number, name, link.
  * No difficulty. Neither publishes one in a form worth mapping onto the
    forum's Easy/Medium/Hard tags, so posts carry only the platform tag.
"""

import asyncio
import re
from dataclasses import dataclass
from html import unescape

import discord
from aiohttp import ClientTimeout

from .config import CSES_EMOJI, EULER_EMOJI, LEETCODE_PROBLEMS_CHANNEL_ID
from .database import site_problem_get, site_problem_save
from .logbus import log_error

_TIMEOUT = ClientTimeout(total=20)
# Both sites serve plain pages, but a default aiohttp agent gets a worse
# response from some hosts than a browser one — same UA the recap scrapes with.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


@dataclass(frozen=True)
class _Site:
    label: str          # human name, also the forum tag's name
    emblem: str         # application emoji, shown on the card
    colour: int         # embed colour, approximating the site's own
    url_re: re.Pattern  # problem url -> ref
    title_re: re.Pattern
    url_fmt: str


SITES: dict[str, _Site] = {
    "cses": _Site(
        label="CSES",
        emblem=CSES_EMOJI,
        colour=0x1B6AC6,
        url_re=re.compile(r"cses\.fi/problemset/task/(\d+)", re.I),
        title_re=re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I),
        url_fmt="https://cses.fi/problemset/task/{ref}",
    ),
    "euler": _Site(
        label="Project Euler",
        emblem=EULER_EMOJI,
        colour=0x6B5B95,
        url_re=re.compile(r"projecteuler\.net/problem=(\d+)", re.I),
        title_re=re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I),
        url_fmt="https://projecteuler.net/problem={ref}",
    ),
}

_BARE_RE = re.compile(r"^#?(\d+)$")
_lock = asyncio.Lock()


def parse_ref(site_key: str, text: str) -> str | None:
    """The problem number from that site's url or a bare number, else None."""
    site = SITES[site_key]
    text = (text or "").strip().strip("<>")
    if not text:
        return None
    m = site.url_re.search(text)
    if m:
        return m.group(1)
    # A bare number only means anything for the site the command names, which
    # is why parsing takes the site rather than sniffing it from the input.
    bare = _BARE_RE.match(text)
    return bare.group(1) if bare else None


def problem_url(site_key: str, ref: str) -> str:
    return SITES[site_key].url_fmt.format(ref=ref)


def _strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


async def fetch_name(session, site_key: str, ref: str) -> str | None:
    """The problem's name, scraped from its page. None if there's no such problem.

    Doubles as the existence check, and each site denies one its own way: CSES
    404s, while Project Euler answers 200 and redirects to its archives page —
    which has a heading, so scraping it blindly would name a post after it.
    Landing anywhere other than the requested URL therefore counts as missing.
    """
    site = SITES[site_key]
    url = problem_url(site_key, ref)
    try:
        async with session.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT) as r:
            if r.status != 200 or str(r.url).rstrip("/") != url.rstrip("/"):
                return None
            body = await r.text()
    except Exception as e:
        log_error(f"[{site.label.upper()}] name lookup failed for {ref}: {e!r}")
        return None
    m = site.title_re.search(body)
    name = _strip_tags(m.group(1)) if m else ""
    return name or None


def build_embed(site_key: str, ref: str, name: str) -> discord.Embed:
    site = SITES[site_key]
    return discord.Embed(
        title=f"{ref}. {name}"[:256],
        url=problem_url(site_key, ref),
        description=f"-# {site.emblem} {site.label}",
        color=site.colour,
    )


async def get_or_create_problem_post(bot, site_key: str, text: str) -> tuple[int | None, str]:
    """Look up or create the forum post for a CSES / Project Euler problem.

    `text` may be a problem URL or a bare number. Returns (thread_id, error).
    """
    site = SITES[site_key]
    ref = parse_ref(site_key, text)
    if not ref:
        return None, f"not a {site.label} problem link"
    if not bot.http_session:
        return None, "http session not ready"

    existing = site_problem_get(site_key, ref)
    if existing:
        return existing["thread_id"], ""

    async with _lock:
        existing = site_problem_get(site_key, ref)
        if existing:
            return existing["thread_id"], ""

        name = await fetch_name(bot.http_session, site_key, ref)
        if not name:
            return None, f"{site.label} problem {ref} doesn't exist"

        forum = (bot.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
                 or await bot.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID))
        if not isinstance(forum, discord.ForumChannel):
            return None, "problems channel is not a forum channel"

        # Platform tag only, for the reason the Codeforces posts carry one:
        # Discord orders a thread's tags itself, and difficulty isn't published
        # by either site anyway. A missing tag doesn't block the post.
        tags = [t for t in forum.available_tags if t.name == site.label]

        try:
            result = await forum.create_thread(
                name=f"{ref}. {name}"[:100],
                embed=build_embed(site_key, ref, name),
                applied_tags=tags,
                reason=f"{site.label} problem post for {ref}",
            )
        except Exception as e:
            log_error(f"[{site.label.upper()}] forum post failed for {ref}: {e!r}")
            return None, f"could not create the post: {e}"

        thread = result.thread if hasattr(result, "thread") else result
        site_problem_save(site_key, ref, name, thread.id, problem_url(site_key, ref))
        return thread.id, ""
