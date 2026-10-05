"""Stream-card banners: each emote from ~/emotes, the Twitch logo bottom-right,
the artist credit bottom-left. The bot uploads one at random with every
go-live card (STREAM_ALERT_IMAGE points at the folder). Rerun when the emotes
change; Pillow comes along for the run only, not as a bot dependency:

    uv run --with pillow python scripts/make_stream_banners.py
"""
import glob, os, sys
from PIL import Image, ImageDraw, ImageFont
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "stream_alerts")
os.makedirs(out, exist_ok=True)
W, H = 896, 504                                      # 2x the 448x252 emotes, crisp on Discord
logo = Image.open("/root/discord-bot/assets/emoji/twitch.png").convert("RGBA")
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)   # small and quiet: a credit, not a caption
for f in sorted(glob.glob(os.path.expanduser("~/emotes/*.png"))):
    em = Image.open(f).convert("RGBA")
    # Scale to cover W x H, then centre-crop: older 2:1 emotes lose a little
    # off the sides rather than gaining empty bands top and bottom.
    k = max(W / em.width, H / em.height)
    sw, sh = round(em.width * k), round(em.height * k)
    img = em.resize((sw, sh), Image.LANCZOS).crop(((sw - W) // 2, (sh - H) // 2, (sw - W) // 2 + W, (sh - H) // 2 + H))
    pad = 18
    lg = logo.resize((92, 92), Image.LANCZOS)
    img.alpha_composite(lg, (W - lg.width - pad, H - lg.height - pad))
    d = ImageDraw.Draw(img)
    text = "art: @pengukim"
    tb = d.textbbox((0, 0), text, font=font)
    d.text((pad, H - pad - tb[3]), text, font=font, fill=(255, 255, 255, 230))
    img.save(os.path.join(out, os.path.basename(f)))
print(len(os.listdir(out)), "banners in", out)
