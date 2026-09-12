"""Time every user has spent in every voice channel.

Reads voice_visits, which records one row per user per channel stint for
everyone in any voice channel. Open stints count up to now. Names are resolved
through Discord's REST API; channels since deleted and members who have left
fall back to their id.

Run from the repo root:
  uv run python scripts/voice_time.py                 everyone, every channel
  uv run python scripts/voice_time.py --since 2026-09-01
  uv run python scripts/voice_time.py --user 287484582943784962
  uv run python scripts/voice_time.py --channel 1482589316520739077
  uv run python scripts/voice_time.py --csv > voice_time.csv
"""

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime

import aiohttp

sys.path.insert(0, ".")

from bot.config import GUILD_ID, TOKEN  # noqa: E402
from bot.database import _db  # noqa: E402

API = "https://discord.com/api/v10"


async def _names() -> tuple[dict[int, str], dict[int, str]]:
    """Channel and member display names, best effort — ids stand in on failure."""
    chans, members = {}, {}
    headers = {"Authorization": f"Bot {TOKEN}"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(f"{API}/guilds/{GUILD_ID}/channels") as r:
                for c in await r.json():
                    chans[int(c["id"])] = c["name"]
            after = "0"
            while True:
                async with s.get(f"{API}/guilds/{GUILD_ID}/members",
                                 params={"limit": 1000, "after": after}) as r:
                    page = await r.json()
                if not isinstance(page, list) or not page:
                    break
                for m in page:
                    u = m["user"]
                    members[int(u["id"])] = m.get("nick") or u.get("global_name") or u["username"]
                if len(page) < 1000:
                    break
                after = page[-1]["user"]["id"]
    except Exception as e:
        print(f"(names unavailable: {e!r} — showing ids)", file=sys.stderr)
    return chans, members


def _totals(since: int, user: int | None, channel: int | None) -> list[tuple[int, int, int, int]]:
    """(user_id, channel_id, seconds, visits), largest first."""
    where, args = ["started_at >= ?"], [since]
    if user:
        where.append("user_id = ?"); args.append(user)
    if channel:
        where.append("channel_id = ?"); args.append(channel)
    with _db() as conn:
        return conn.execute(
            "SELECT user_id, channel_id, "
            "  SUM(seconds) + SUM(CASE WHEN left_at IS NULL THEN MAX(0, ? - last_join_at) ELSE 0 END), "
            "  COUNT(*) "
            f"FROM voice_visits WHERE {' AND '.join(where)} "
            "GROUP BY user_id, channel_id ORDER BY 3 DESC",
            (int(time.time()), *args)).fetchall()


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="YYYY-MM-DD; only stints that started on or after it")
    ap.add_argument("--user", type=int, help="one Discord user id")
    ap.add_argument("--channel", type=int, help="one voice channel id")
    ap.add_argument("--csv", action="store_true", help="machine-readable, one row per user+channel")
    a = ap.parse_args()

    since = int(datetime.strptime(a.since, "%Y-%m-%d").timestamp()) if a.since else 0
    rows = [r for r in _totals(since, a.user, a.channel) if (r[2] or 0) > 0]
    chans, members = asyncio.run(_names())
    cname = lambda c: chans.get(c, f"(deleted {c})")
    uname = lambda u: members.get(u, str(u))

    if a.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["user_id", "user", "channel_id", "channel", "seconds", "minutes", "visits"])
        for u, c, secs, n in rows:
            w.writerow([u, uname(u), c, cname(c), secs, secs // 60, n])
        return

    # Grouped by person: their total, then where that time went.
    by_user: dict[int, list] = {}
    for u, c, secs, n in rows:
        by_user.setdefault(u, []).append((c, secs, n))
    order = sorted(by_user, key=lambda u: -sum(s for _, s, _ in by_user[u]))
    print(f"{len(order)} people · {len(rows)} user/channel pairs"
          + (f" · since {a.since}" if a.since else " · all time") + "\n")
    for u in order:
        total = sum(s for _, s, _ in by_user[u])
        print(f"{uname(u):<28} {_hm(total):>9}")
        for c, secs, n in by_user[u]:
            print(f"    #{cname(c):<30} {_hm(secs):>9}   {n} visit{'s' if n != 1 else ''}")


if __name__ == "__main__":
    main()
