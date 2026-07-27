"""Bring existing problem posts up to the current card format.

For every thread in leetcode_problems:
  * apply the LeetCode forum tag (the logo lives there, so the card itself
    carries no thumbnail)
  * rebuild the header line as "<emoji> **Difficulty**[ · rating][ 🔒 Premium]",
    filling in the zerotrac rating if the problem has one
  * strip the thumbnail/author-icon experiments from earlier passes

Idempotent — a thread already in the target state is skipped, so the pass can be
re-run after an interruption and picks up where it left off.

Most problem threads are archived and Discord refuses edits in an archived
thread, so each one is unarchived, edited, then re-archived. Those channel edits
share a 10-per-15s bucket, hence the pacing: budget ~3s per archived thread.

Run from the repo root:
    uv run python scripts/backfill_problem_cards.py            # dry run
    uv run python scripts/backfill_problem_cards.py --apply
    uv run python scripts/backfill_problem_cards.py --apply --limit 25
"""

import asyncio
import re
import sys

import aiohttp

sys.path.insert(0, ".")

from bot.config import (  # noqa: E402
    GUILD_ID, LEETCODE_PROBLEMS_CHANNEL_ID, TOKEN,
)
from bot.database import _db, zerotrac_cache_get_all  # noqa: E402

APPLY = "--apply" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])

API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {TOKEN}",
           "User-Agent": "DiscordBot (card-backfill) Python/3.12",
           "Content-Type": "application/json"}
PACE = 3.1          # seconds per archived thread (channel bucket: 10 per 15s)
PACE_ACTIVE = 0.4   # no channel edits needed

# "<:leetcode:123> 🟡 **Medium** · 1337 🔒 Premium" in any partial state
HEAD_RE = re.compile(
    r"^(?:<:\w+:\d+>\s*)?(\S+ \*\*(?:Easy|Medium|Hard|Unknown)\*\*)"
    r"(?:\s*·\s*(?:⭐\s*)?(?:\d+|Unrated))?(.*)$")


async def request(session, method, url, **kw):
    """One call, honouring 429s. Returns (status, json_or_None)."""
    for _ in range(5):
        async with session.request(method, url, **kw) as r:
            if r.status == 429:
                body = await r.json()
                await asyncio.sleep(float(body.get("retry_after", 2)) + 0.5)
                continue
            if r.status == 204:
                return r.status, None
            try:
                return r.status, await r.json()
            except Exception:
                return r.status, None
    return 429, None


async def active_thread_ids(session):
    status, js = await request(session, "GET", f"{API}/guilds/{GUILD_ID}/threads/active")
    if status != 200 or not js:
        return set()
    return {int(t["id"]) for t in js.get("threads", [])
            if int(t.get("parent_id", 0)) == LEETCODE_PROBLEMS_CHANNEL_ID}


def target_embed(embed, rating):
    """The embed as it should look, or None if it already looks like that."""
    lines = (embed.get("description") or "").split("\n")
    if not lines:
        return None
    hit = HEAD_RE.match(lines[0])
    if not hit:
        return None
    core, tail = hit.group(1), hit.group(2)
    want_line = core + (f" · {round(rating)}" if rating else "") + tail
    clean = not embed.get("thumbnail") and not embed.get("author")
    if want_line == lines[0] and clean:
        return None
    out = dict(embed)
    out["description"] = "\n".join(lines)
    out.pop("thumbnail", None)   # the LeetCode tag carries the logo now
    out.pop("author", None)
    return out


async def leetcode_tag_id(session):
    status, ch = await request(session, "GET", f"{API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}")
    if status != 200 or not ch:
        return None
    for t in ch.get("available_tags", []):
        if t["name"] == "LeetCode":
            return t["id"]
    return None


async def main():
    ratings = zerotrac_cache_get_all()
    with _db() as conn:
        problems = conn.execute(
            "SELECT thread_id, title_slug, question_id FROM leetcode_problems "
            "ORDER BY CAST(question_id AS INTEGER) DESC").fetchall()

    async with aiohttp.ClientSession(headers=HEADERS) as s:
        active = await active_thread_ids(s)
        tag_id = await leetcode_tag_id(s)
        if not tag_id:
            print("no 'LeetCode' tag on the forum — create it first")
            return
        print(f"problem threads: {len(problems)} | active: {len(active)} | "
              f"archived: {len(problems) - len(active)} | LeetCode tag {tag_id}", flush=True)
        if not APPLY:
            rated = sum(1 for _, slug, _ in problems if ratings.get(slug, {}).get("rating"))
            print(f"would tag all of them LeetCode, and fill in a rating on {rated}")
            est = (len(problems) - len(active)) * PACE / 60
            print(f"estimated runtime: ~{est:.0f} min")
            print("\ndry run — re-run with --apply")
            return

        done = skipped = failed = 0
        todo = problems[:LIMIT] if LIMIT else problems
        for i, (tid, slug, qid) in enumerate(todo, 1):
            rating = ratings.get(slug, {}).get("rating")
            is_archived = tid not in active

            st, thread = await request(s, "GET", f"{API}/channels/{tid}")
            if st != 200 or not thread:
                failed += 1
                print(f"  {qid}: thread unreadable ({st})", flush=True)
                await asyncio.sleep(PACE_ACTIVE)
                continue
            tags = list(thread.get("applied_tags") or [])
            needs_tag = tag_id not in tags

            status, msg = await request(s, "GET", f"{API}/channels/{tid}/messages/{tid}")
            if status != 200 or not msg or not msg.get("embeds"):
                failed += 1
                print(f"  {qid}: starter unreadable ({status})", flush=True)
                await asyncio.sleep(PACE_ACTIVE)
                continue

            new_embed = target_embed(msg["embeds"][0], rating)
            if new_embed is None and not needs_tag:
                skipped += 1
                await asyncio.sleep(0.05)
                continue

            # Unarchiving and tagging ride in one call, so tagging an archived
            # thread costs nothing beyond the unarchive we already needed.
            if is_archived or needs_tag:
                payload = {}
                if is_archived:
                    payload["archived"] = False
                if needs_tag:
                    payload["applied_tags"] = tags + [tag_id]
                st, _ = await request(s, "PATCH", f"{API}/channels/{tid}", json=payload)
                if st >= 300:
                    failed += 1
                    print(f"  {qid}: thread update failed ({st})", flush=True)
                    await asyncio.sleep(PACE)
                    continue

            if new_embed is not None:
                embeds = [new_embed] + msg["embeds"][1:]
                st, _ = await request(s, "PATCH", f"{API}/channels/{tid}/messages/{tid}",
                                      json={"embeds": embeds})
                if st >= 300:
                    failed += 1
                    print(f"  {qid}: edit failed ({st})", flush=True)
                else:
                    done += 1
            else:
                done += 1

            if is_archived:
                await request(s, "PATCH", f"{API}/channels/{tid}", json={"archived": True})

            if i % 50 == 0:
                print(f"  {i}/{len(todo)} — updated {done}, skipped {skipped}, "
                      f"failed {failed}", flush=True)
            await asyncio.sleep(PACE if is_archived else PACE_ACTIVE)

        print(f"updated {done}, already current {skipped}, failed {failed}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
