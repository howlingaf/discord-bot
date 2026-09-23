"""Create the Twitch emoji and the #problems-archive "Twitch" tag, once.

The tag marks a problem that was done live on stream (bot/streamwork.py adds
it, and takes it off again when the VOD expires). Discord forum tags take a
guild emoji, so the glyph is uploaded to the guild alongside the platform
logos already there.

The glyph is drawn rather than downloaded: Twitch ships it as SVG or a 32px
favicon, and it is all straight lines, so drawing it gives a clean 128px mark
with no upscaling.

    uv run --with pillow python scripts/twitch_tag.py          # show what exists
    uv run --with pillow python scripts/twitch_tag.py --apply  # create both
"""

import base64
import io
import json
import sys
import urllib.request

sys.path.insert(0, ".")

from bot.config import GUILD_ID, LEETCODE_PROBLEMS_CHANNEL_ID, TOKEN  # noqa: E402

API = "https://discord.com/api/v10"
PURPLE = (145, 70, 255, 255)
EMOJI_NAME = "twitch"
TAG_NAME = "Twitch"
ASSET = "assets/emoji/twitch.png"

# The glitch, in its own 2400x2800 coordinate space: outer body, the notch cut
# out of it, and the two eyes.
BODY = [(500, 0), (0, 500), (0, 2300), (600, 2300), (600, 2800), (1100, 2300),
        (1500, 2300), (2400, 1400), (2400, 0)]
NOTCH = [(2200, 1300), (1800, 1700), (1400, 1700), (1050, 2050), (1050, 1700),
         (600, 1700), (600, 200), (2200, 200)]
EYES = [[(1150, 550), (1350, 550), (1350, 1150), (1150, 1150)],
        [(1700, 550), (1900, 550), (1900, 1150), (1700, 1150)]]


def glyph(size: int = 128) -> bytes:
    from PIL import Image, ImageDraw
    scale = 4                       # draw large, then downscale for smooth edges
    w, h = 2400, 2800
    im = Image.new("RGBA", (w // scale, h // scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(x / scale, y / scale) for x, y in BODY], fill=PURPLE)
    d.polygon([(x / scale, y / scale) for x, y in NOTCH], fill=(0, 0, 0, 0))
    for eye in EYES:
        d.polygon([(x / scale, y / scale) for x, y in eye], fill=PURPLE)
    # Square canvas: Discord scales emoji to a square, and a tall glyph would
    # otherwise be squashed.
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    out = io.BytesIO()
    canvas.resize((size, size), Image.LANCZOS).save(out, format="PNG")
    return out.getvalue()


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json",
                 "User-Agent": "howlingaf-tools"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def main() -> None:
    apply = "--apply" in sys.argv
    png = glyph()
    with open(ASSET, "wb") as f:
        f.write(png)
    print(f"{ASSET}: {len(png)} bytes")

    emoji = next((e for e in api("GET", f"/guilds/{GUILD_ID}/emojis") if e["name"] == EMOJI_NAME), None)
    if emoji:
        print(f"emoji :{EMOJI_NAME}: exists ({emoji['id']})")
    elif apply:
        emoji = api("POST", f"/guilds/{GUILD_ID}/emojis",
                    {"name": EMOJI_NAME, "image": "data:image/png;base64," + base64.b64encode(png).decode()})
        print(f"emoji :{EMOJI_NAME}: created ({emoji['id']})")
    else:
        print(f"emoji :{EMOJI_NAME}: missing (run with --apply)")
        return

    forum = api("GET", f"/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}")
    tags = forum.get("available_tags") or []
    tag = next((t for t in tags if t["name"] == TAG_NAME), None)
    if tag:
        print(f"tag {TAG_NAME!r} exists ({tag['id']}, emoji {tag.get('emoji_id')})")
        return
    if not apply:
        print(f"tag {TAG_NAME!r} missing (run with --apply)")
        return
    updated = api("PATCH", f"/channels/{LEETCODE_PROBLEMS_CHANNEL_ID}",
                  {"available_tags": tags + [{"name": TAG_NAME, "emoji_id": emoji["id"],
                                              "emoji_name": None, "moderated": False}]})
    tag = next(t for t in (updated.get("available_tags") or []) if t["name"] == TAG_NAME)
    print(f"tag {TAG_NAME!r} created ({tag['id']})")


if __name__ == "__main__":
    main()
