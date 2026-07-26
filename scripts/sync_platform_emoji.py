"""Create/replace the platform logo APPLICATION emoji used by the recap card.

Application emoji belong to the bot itself: usable in any server it's in, and
they don't consume a guild's 50 emoji slots. After running this, paste the
printed ids into PLATFORM_EMBLEMS in bot/recap.py.

Logos are fetched from each platform, padded to a square (several ship only a
wide wordmark) and downscaled to 128x128.

Run from the repo root:
    uv run python scripts/sync_platform_emoji.py            # list current
    uv run python scripts/sync_platform_emoji.py --create   # create missing
    uv run python scripts/sync_platform_emoji.py --replace  # delete + recreate
"""

import asyncio
import base64
import io
import sys

import aiohttp
from PIL import Image

sys.path.insert(0, ".")

from bot.config import TOKEN  # noqa: E402

# Source logo per platform — a url, or a path under the repo for marks we build
# ourselves. Prefer a square mark; a wordmark gets padded.
#
# CSES ships only a 4:1 wordmark, which shrinks to unreadable at Discord's ~22px
# inline size, so assets/emoji/cses.png is a stacked CS/ES lettermark instead.
SOURCES = {
    "leetcode": "https://leetcode.com/static/images/LeetCode_logo_rvs.png",
    "codeforces": "https://codeforces.org/s/0/favicon-96x96.png",
    "projecteuler": "https://projecteuler.net/favicons/apple-touch-icon.png",
    "cses": "assets/emoji/cses.png",
}

_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_API = "https://discord.com/api/v10"


def _squarify(raw: bytes, size: int = 128) -> bytes:
    """Centre the logo on a transparent square so it survives emoji scaling."""
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    out = io.BytesIO()
    canvas.resize((size, size), Image.LANCZOS).save(out, format="PNG")
    return out.getvalue()


async def main():
    create = "--create" in sys.argv
    replace = "--replace" in sys.argv

    async with aiohttp.ClientSession() as web:
        headers = {"Authorization": f"Bot {TOKEN}",
                   "User-Agent": f"DiscordBot (recap-emoji-sync) {_BROWSER_UA}"}
        async with aiohttp.ClientSession(headers=headers) as api:
            async with api.get(f"{_API}/users/@me") as r:
                app_id = (await r.json())["id"]
            async with api.get(f"{_API}/applications/{app_id}/emojis") as r:
                existing = {e["name"]: e["id"] for e in (await r.json()).get("items", [])}

            for name, url in SOURCES.items():
                if name in existing and not replace:
                    print(f"{name:14} exists  <:{name}:{existing[name]}>"
                          f"{'' if create else ' (use --replace to refresh)'}")
                    continue
                if not (create or replace):
                    print(f"{name:14} missing (use --create)")
                    continue

                if name in existing and replace:
                    async with api.delete(
                            f"{_API}/applications/{app_id}/emojis/{existing[name]}") as r:
                        print(f"{name:14} deleted {existing[name]} ({r.status})")

                if "://" in url:
                    async with web.get(url, headers={"User-Agent": _BROWSER_UA}) as r:
                        raw = await r.read()
                else:
                    raw = open(url, "rb").read()
                payload = {"name": name,
                           "image": "data:image/png;base64," + base64.b64encode(
                               _squarify(raw)).decode()}
                async with api.post(f"{_API}/applications/{app_id}/emojis",
                                    json=payload) as r:
                    js = await r.json()
                    if r.status >= 300:
                        print(f"{name:14} FAILED {r.status} {js}")
                    else:
                        print(f"{name:14} created <:{name}:{js['id']}>")


if __name__ == "__main__":
    asyncio.run(main())
