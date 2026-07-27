"""One-off: delete every premium problem post and the premium forum tags.

Premium problems are LeetCode-paywalled, so their forum threads carry a
statement most members can't act on. This removes:

  * every thread in the problems forum tagged "Premium" or "Weekly Premium"
  * their rows in leetcode_problems — otherwise /lc <id> would hand back a
    link to a thread that no longer exists
  * both tags from the forum's tag list

Run from the repo root:
    uv run python scripts/purge_premium_problems.py            # dry run
    uv run python scripts/purge_premium_problems.py --apply
"""

import asyncio
import sys

import aiohttp

sys.path.insert(0, ".")

from bot.config import LEETCODE_PROBLEMS_CHANNEL_ID, TOKEN  # noqa: E402
from bot.database import _db  # noqa: E402

APPLY = "--apply" in sys.argv
TAG_NAMES = ("Premium", "Weekly Premium")
PACE = 1.5  # seconds between deletes; thread deletion is rate limited hard

API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {TOKEN}",
           "User-Agent": "DiscordBot (premium-purge) Python/3.12",
           "Content-Type": "application/json"}


async def forum_tags(session):
    async with session.get(f"{API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}") as r:
        ch = await r.json()
    return ch.get("available_tags", [])


async def threads_with_tag(session, tag_id):
    """Every thread carrying `tag_id`, archived included, via forum search."""
    found, offset = {}, 0
    while True:
        url = (f"{API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}/threads/search"
               f"?tag={tag_id}&limit=25&offset={offset}")
        async with session.get(url) as r:
            if r.status == 429:
                await asyncio.sleep(float((await r.json()).get("retry_after", 2)) + 0.5)
                continue
            if r.status != 200:
                print(f"  search failed: HTTP {r.status}")
                break
            js = await r.json()
        batch = js.get("threads") or []
        for t in batch:
            found[t["id"]] = t["name"]
        offset += len(batch)
        if not batch or offset >= (js.get("total_results") or 0):
            break
    return found


async def delete_thread(session, tid):
    for _ in range(4):
        async with session.delete(f"{API}/channels/{tid}") as r:
            if r.status == 429:
                await asyncio.sleep(float((await r.json()).get("retry_after", 2)) + 0.5)
                continue
            return r.status < 300 or r.status == 404
    return False


async def main():
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        tags = await forum_tags(s)
        targets = {t["name"]: t["id"] for t in tags if t["name"] in TAG_NAMES}
        if not targets:
            print("neither tag exists on the forum — nothing to do")
            return

        threads = {}
        for name, tid in targets.items():
            hits = await threads_with_tag(s, tid)
            print(f"{name!r} (tag {tid}): {len(hits)} thread(s)")
            threads.update(hits)

        print(f"\nunique threads to delete: {len(threads)}")
        for tid, name in list(threads.items())[:8]:
            print("   ", tid, name)
        if len(threads) > 8:
            print(f"    …and {len(threads) - 8} more")

        with _db() as conn:
            ids = list(threads)
            marks = ",".join("?" * len(ids)) or "NULL"
            rows = conn.execute(
                f"SELECT COUNT(*) FROM leetcode_problems WHERE thread_id IN ({marks})",
                ids).fetchone()[0]
        print(f"leetcode_problems rows referencing them: {rows}")
        print(f"tags to delete: {', '.join(targets)}")

        if not APPLY:
            print("\ndry run — re-run with --apply")
            return

        deleted = 0
        for i, tid in enumerate(threads, 1):
            if await delete_thread(s, tid):
                deleted += 1
            else:
                print(f"  failed to delete {tid}", flush=True)
            if i % 20 == 0:
                print(f"  {i}/{len(threads)}…", flush=True)
            await asyncio.sleep(PACE)
        print(f"deleted {deleted}/{len(threads)} thread(s)")

        with _db() as conn:
            ids = list(threads)
            marks = ",".join("?" * len(ids)) or "NULL"
            gone = conn.execute(
                f"DELETE FROM leetcode_problems WHERE thread_id IN ({marks})", ids).rowcount
            conn.commit()
        print(f"removed {gone} leetcode_problems row(s)")

        keep = [t for t in tags if t["name"] not in TAG_NAMES]
        async with s.patch(f"{API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}",
                           json={"available_tags": keep}) as r:
            print(f"tag removal: HTTP {r.status}")


if __name__ == "__main__":
    asyncio.run(main())
