import re
from html import unescape
from urllib.parse import urlparse

import discord
from aiohttp import ClientSession, ClientTimeout

from .config import (
    GUILD_ID,
    LEETCODE_BASE,
    LEETCODE_SUBMISSIONS_URL,
    LEETCODE_RECAP_CHANNEL_ID,
    STREAMER_NAME,
    STREAMER_DISCORD_ID,
)
from .database import leetcode_get_problem_by_slug, leetcode_save_problem
from .leetcode import (
    DIFF_COLORS,
    DIFF_EMOJI,
    get_or_create_problem_post,
    fetch_leetcode_problem,
)
from .twitchlink import solution_name, maybe_prompt


# The recap is DSA-only: links the streamer shares on stream are dropped unless
# they point at one of these sites. leetcode.com is deliberately absent — the
# twitch-bot's _RECAP_SKIP_HOSTS strips it before the payload reaches us, since
# LeetCode problems already enter the recap as forum links via the submission
# path and would otherwise be listed twice.
DSA_LINK_DOMAINS = (
    "codeforces.com",
    "projecteuler.net",
    "cses.fi",
)

# Emblem shown next to each problem/link in the recap card. These are the real
# platform logos, uploaded as APPLICATION emoji (owned by the bot, usable in any
# server, no guild emoji slots consumed) — see scripts/sync_platform_emoji.py.
PLATFORM_EMBLEMS = {
    "leetcode.com": "<:leetcode:1530820116667957298>",
    "codeforces.com": "<:codeforces:1530820117225672890>",
    "projecteuler.net": "<:projecteuler:1530820117879980144>",
    "cses.fi": "<:cses:1530822475691196497>",
}
_DEFAULT_EMBLEM = "🔗"

# Pull a human problem reference out of each platform's URL shape.
_LINK_PATTERNS = (
    ("codeforces.com", re.compile(r"/problemset/problem/(\d+)/(\w+)", re.I),
     lambda m: f"Codeforces {m.group(1)}{m.group(2).upper()}"),
    ("codeforces.com", re.compile(r"/contest/(\d+)/problem/(\w+)", re.I),
     lambda m: f"Codeforces {m.group(1)}{m.group(2).upper()}"),
    ("codeforces.com", re.compile(r"/gym/(\d+)/problem/(\w+)", re.I),
     lambda m: f"Codeforces gym {m.group(1)}{m.group(2).upper()}"),
    ("projecteuler.net", re.compile(r"problem=(\d+)", re.I),
     lambda m: f"Project Euler {m.group(1)}"),
    ("cses.fi", re.compile(r"/task/(\d+)", re.I),
     lambda m: f"CSES {m.group(1)}"),
    ("leetcode.com", re.compile(r"/problems/([a-z0-9-]+)", re.I),
     lambda m: m.group(1).replace("-", " ").title()),
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def is_dsa_link(url: str) -> bool:
    """True if url is an http(s) link to one of the DSA sites (subdomains included)."""
    if not isinstance(url, str):
        return False
    raw = url.strip().strip("<>")
    if not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        parts = urlparse(raw)
        if parts.scheme not in ("http", "https"):
            return False
        # Reject credentials — a real DSA link never carries them, and they let
        # a non-http scheme (mailto:me@host) survive the https:// prefix above.
        if parts.username or parts.password:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in DSA_LINK_DOMAINS)


def filter_dsa_links(links: list[str]) -> list[str]:
    return [u for u in links if is_dsa_link(u)]


def _link_host(url: str) -> str:
    raw = url.strip().strip("<>")
    if not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        return (urlparse(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def platform_emblem(url: str) -> str:
    host = _link_host(url)
    for domain, emblem in PLATFORM_EMBLEMS.items():
        if host == domain or host.endswith("." + domain):
            return emblem
    return _DEFAULT_EMBLEM


def link_line(url: str, title: str | None = None) -> str:
    """'🔷 [Codeforces 1421A — XORwice](url)' — emblem, reference, problem name.

    Markdown links inside an embed don't generate a preview card.
    """
    clean = url.strip().strip("<>")
    if not _SCHEME_RE.match(clean):
        clean = "https://" + clean  # markdown links need a scheme to resolve
    host = _link_host(clean)
    label = ""
    for domain, pattern, build in _LINK_PATTERNS:
        if host == domain or host.endswith("." + domain):
            m = pattern.search(clean)
            if m:
                label = build(m)
                break
    if not label:
        label = host or clean
    if title and title.lower() not in label.lower():
        label = f"{label} — {title}"
    # Keep the markdown link intact: strip ] from the label and percent-encode
    # parens in the href so a url can't terminate the link early.
    href = clean.replace("(", "%28").replace(")", "%29")
    return f"{platform_emblem(clean)} [{label.replace(']', '')}]({href})"


# Problem-name lookup per platform. Best effort throughout: a failed fetch just
# leaves the entry showing its reference (e.g. "Codeforces 1421A") on its own.
_CF_API = "https://codeforces.com/api/problemset.problems"
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_TITLE_TIMEOUT = 20
# Where each platform keeps the problem name in its page HTML.
_TITLE_TAGS = {
    "projecteuler.net": re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I),
    "cses.fi": re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I),
}
_CF_REF_RE = re.compile(
    r"/problemset/problem/(\d+)/(\w+)|/contest/(\d+)/problem/(\w+)", re.I)


def _cf_ref(url: str) -> str | None:
    """'1421A' from either codeforces url shape (gym problems aren't in the API)."""
    m = _CF_REF_RE.search(url)
    if not m:
        return None
    contest, index = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return f"{contest}{index.upper()}"


def _strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()


async def fetch_problem_titles(session: ClientSession, urls: list[str]) -> dict[str, str]:
    """Map url -> problem name for the platforms that expose one."""
    titles: dict[str, str] = {}
    headers = {"User-Agent": _BROWSER_UA}
    timeout = ClientTimeout(total=_TITLE_TIMEOUT)

    cf = {u: ref for u in urls if (ref := _cf_ref(u)) and "codeforces.com" in _link_host(u)}
    if cf:
        try:
            async with session.get(_CF_API, headers=headers, timeout=timeout) as resp:
                data = await resp.json(content_type=None) if resp.status == 200 else {}
            by_ref = {f"{p.get('contestId')}{p.get('index')}": p.get("name")
                      for p in (data.get("result") or {}).get("problems") or []}
            for url, ref in cf.items():
                if by_ref.get(ref):
                    titles[url] = by_ref[ref]
        except Exception as e:
            print(f"[RECAP] Codeforces name lookup failed: {e!r}")

    for url in urls:
        if url in titles:
            continue
        host = _link_host(url)
        pattern = next((p for d, p in _TITLE_TAGS.items()
                        if host == d or host.endswith("." + d)), None)
        if not pattern:
            continue
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                m = pattern.search(await resp.text())
            if m:
                name = _strip_tags(m.group(1))
                if name:
                    titles[url] = name
        except Exception as e:
            print(f"[RECAP] name lookup failed for {url}: {e!r}")

    return titles


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
    raw_links = payload.get("streamer_links") or []
    streamer_links = filter_dsa_links(raw_links)
    dropped = len(raw_links) - len(streamer_links)
    if dropped:
        print(f"[RECAP] Dropped {dropped} non-DSA link(s) from recap")

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
            streamer_name = f"<@{STREAMER_DISCORD_ID}>" if STREAMER_DISCORD_ID else f"**{STREAMER_NAME}**"
            line = f"{streamer_name} submitted a solution!"
            if sub_url:
                line += f"\n<{sub_url}>"
            lines.append(line)

        for cs in entries["chatters"]:
            twitch_user = cs.get("twitch_user") or "anonymous"
            url = cs.get("url") or ""
            await maybe_prompt(bot, twitch_user)  # new handle -> mod approval prompt
            line = f"{solution_name(twitch_user)} submitted a solution!"
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
        await _post_recap_message(bot, session, recap_entries, streamer_links)


async def _post_recap_message(bot, session: ClientSession, entries: list[dict],
                              streamer_links: list[str]):
    """Build and send the recap embed in the recap channel."""
    channel = bot.get_channel(LEETCODE_RECAP_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(LEETCODE_RECAP_CHANNEL_ID)
        except Exception as e:
            print(f"[RECAP] Could not fetch recap channel: {e}")
            return

    # One embed, one list: everything worked on during the stream is a problem,
    # whether it came from the LeetCode feed or a link shared on stream, so they
    # all sit together — grouped by platform, blank line between entries.
    # Markdown links inside an embed don't preview, so this stays one message.
    leetcode_emblem = PLATFORM_EMBLEMS.get("leetcode.com", _DEFAULT_EMBLEM)
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        thread_url = f"https://discord.com/channels/{GUILD_ID}/{entry['thread_id']}"
        grouped.setdefault("leetcode.com", []).append(
            f"{leetcode_emblem} [{entry['question_id']}. {entry['problem_name']}]({thread_url})"
        )
    titles = await fetch_problem_titles(session, streamer_links) if streamer_links else {}
    for url in streamer_links:
        grouped.setdefault(_link_host(url), []).append(link_line(url, titles.get(url)))

    lines = []
    for host in sorted(grouped, key=_platform_rank):
        lines.extend(grouped[host])

    blocks = _chunk_lines(lines, limit=4096) if lines else []
    embed = discord.Embed(
        title="Stream Recap",
        description=blocks[0] if blocks else None,
        color=0xFFA116,
    )
    # Overflow past the description limit continues in unnamed fields.
    for block in blocks[1:]:
        embed.add_field(name="​", value=block[:1024], inline=False)

    try:
        await channel.send(embed=embed)
        print(f"[RECAP] Recap sent to channel {LEETCODE_RECAP_CHANNEL_ID} "
              f"({len(lines)} problem(s) across {len(grouped)} platform(s))")
    except Exception as e:
        print(f"[RECAP] Failed to send recap message: {e}")


def _platform_rank(host: str) -> tuple[int, str]:
    """Stream problems (LeetCode) first, then the other platforms in a fixed
    order so a recap's grouping doesn't shuffle between streams."""
    order = list(PLATFORM_EMBLEMS)
    for i, domain in enumerate(order):
        if host == domain or host.endswith("." + domain):
            return (i, host)
    return (len(order), host)


def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
    """Group lines into blocks that fit an embed, blank line between entries."""
    blocks, current = [], ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > limit and current:
            blocks.append(current)
            current = line
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks
