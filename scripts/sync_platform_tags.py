"""Create the #problems forum tag for a platform, plus the GUILD emoji it needs.

Forum tags can only carry a guild emoji — the application emoji the recap cards
use (scripts/sync_platform_emoji.py) are not accepted by the tag API — so each
platform ends up with its logo uploaded twice, once per kind. This script owns
the guild half and the tag itself; the LeetCode and Codeforces ones predate it
and are left exactly as they are.

Idempotent: an emoji or tag that already exists is reported and skipped, so a
re-run after adding a platform only creates the missing pieces.

Run from the repo root:
    uv run python scripts/sync_platform_tags.py            # show what's missing
    uv run python scripts/sync_platform_tags.py --apply    # create it
"""

import asyncio
import base64
import sys

import aiohttp

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from sync_platform_emoji import SOURCES, _BROWSER_UA, _squarify  # noqa: E402

from bot.config import TOKEN, GUILD_ID, LEETCODE_PROBLEMS_CHANNEL_ID  # noqa: E402

_API = "https://discord.com/api/v10"

# Forum tag name -> the guild emoji name backing it. The emoji's logo comes from
# SOURCES, so a platform only needs adding in one place.
PLATFORMS = {
    "CSES": "cses",
    "Project Euler": "projecteuler",
}

APPLY = "--apply" in sys.argv


async def _load_image(web: aiohttp.ClientSession, source: str) -> bytes:
    if "://" in source:
        async with web.get(source, headers={"User-Agent": _BROWSER_UA}) as r:
            raw = await r.read()
    else:
        raw = open(source, "rb").read()
    return _squarify(raw)


async def main():
    headers = {"Authorization": f"Bot {TOKEN}"}
    async with aiohttp.ClientSession() as web, \
            aiohttp.ClientSession(headers=headers) as api:

        async with api.get(f"{_API}/guilds/{GUILD_ID}/emojis") as r:
            guild_emoji = {e["name"]: e["id"] for e in await r.json()}
        print(f"guild emoji: {len(guild_emoji)}/50 used")

        for tag_name, emoji_name in PLATFORMS.items():
            if emoji_name in guild_emoji:
                print(f"  emoji {emoji_name:14} exists ({guild_emoji[emoji_name]})")
                continue
            if not APPLY:
                print(f"  emoji {emoji_name:14} MISSING — would upload from "
                      f"{SOURCES[emoji_name]}")
                continue
            image = await _load_image(web, SOURCES[emoji_name])
            payload = {"name": emoji_name,
                       "image": "data:image/png;base64," + base64.b64encode(image).decode()}
            async with api.post(f"{_API}/guilds/{GUILD_ID}/emojis", json=payload) as r:
                js = await r.json()
                if r.status >= 300:
                    print(f"  emoji {emoji_name:14} FAILED {r.status} {js}")
                    return
                guild_emoji[emoji_name] = js["id"]
                print(f"  emoji {emoji_name:14} created {js['id']}")

        async with api.get(f"{_API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}") as r:
            forum = await r.json()
        existing = forum["available_tags"]
        have = {t["name"] for t in existing}
        print(f"forum tags: {len(existing)}/20 used — {', '.join(sorted(have))}")

        # Existing tags are resent verbatim, ids included: this field is
        # replaced wholesale, and a tag dropped from it is deleted from every
        # thread carrying it.
        new_tags = list(existing)
        for tag_name, emoji_name in PLATFORMS.items():
            if tag_name in have:
                print(f"  tag {tag_name:14} exists")
                continue
            if emoji_name not in guild_emoji:
                print(f"  tag {tag_name:14} skipped — emoji not created yet")
                continue
            print(f"  tag {tag_name:14} {'creating' if APPLY else 'MISSING — would create'}"
                  f" with <:{emoji_name}:{guild_emoji[emoji_name]}>")
            new_tags.append({"name": tag_name, "moderated": False,
                             "emoji_id": guild_emoji[emoji_name], "emoji_name": None})

        if len(new_tags) == len(existing):
            print("nothing to do")
            return
        if not APPLY:
            print("\ndry run — re-run with --apply to create")
            return
        if len(new_tags) > 20:
            print(f"refusing: {len(new_tags)} tags exceeds Discord's limit of 20")
            return

        async with api.patch(f"{_API}/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}",
                             json={"available_tags": new_tags}) as r:
            js = await r.json()
            if r.status >= 300:
                print(f"tag write FAILED {r.status} {js}")
                return
            print("tags now: " + ", ".join(f"{t['name']}" for t in js["available_tags"]))


if __name__ == "__main__":
    asyncio.run(main())
