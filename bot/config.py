import os

from dotenv import load_dotenv

load_dotenv()

# ---------------- Discord ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

WEB_BIND_HOST = os.getenv("WEB_BIND_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))

DB_PATH = os.getenv("DB_PATH", "overlay.db")

# ---------------- Spotify ----------------
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_ALLOWED_USER_ID = int(os.getenv("SPOTIFY_ALLOWED_USER_ID", "0"))
SPOTIFY_VOICE_CHANNEL_ID = int(os.getenv("SPOTIFY_VOICE_CHANNEL_ID", "0"))
SPOTIFY_PAUSE_THRESHOLD = int(os.getenv("SPOTIFY_PAUSE_THRESHOLD", "2"))
SPOTIFY_DEBOUNCE_SECONDS = int(os.getenv("SPOTIFY_DEBOUNCE_SECONDS", "0"))

SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state"

# ---------------- LeetCode ----------------
LEETCODE_DAILY_URL = "https://leetcode-api-pied.vercel.app/daily"
LEETCODE_PROBLEM_URL = "https://leetcode-api-pied.vercel.app/problem/{qid}"
LEETCODE_BASE = "https://leetcode.com"

MAX_EXAMPLES = int(os.getenv("LEETCODE_MAX_EXAMPLES", "3"))

STREAMER_NAME = "howlingaf"
# Built from STREAMER_NAME; a dead handle returns 200 and [] — never an error.
LEETCODE_SUBMISSIONS_URL = f"https://leetcode-api-pied.vercel.app/user/{STREAMER_NAME}/submissions"
# The streamer's Discord id (= the bot owner) so streamer solution lines can show
# a silent @mention instead of the plain name. 0 -> fall back to STREAMER_NAME.
STREAMER_DISCORD_ID = SPOTIFY_ALLOWED_USER_ID

# ---------------- LeetCode Problems Forum ----------------
LEETCODE_PROBLEMS_CHANNEL_ID = 1472231552607064144
# Application emoji carrying the LeetCode logo — owned by the bot, so it renders
# in any server it's in and costs no guild emoji slot. Shared by the problem
# cards and the recap's platform emblems (scripts/sync_platform_emoji.py).
LEETCODE_EMOJI = os.getenv("LEETCODE_EMOJI") or "<:leetcode:1530820116667957298>"
LEETCODE_DAILY_NOTIF_CHANNEL_ID = 1472396200409043086

# Application emoji for the other problem sites, same origin as LEETCODE_EMOJI
# (scripts/sync_platform_emoji.py). These are the emblems used in messages; the
# forum tags carry separate GUILD emoji, which Discord requires for tags —
# see scripts/sync_platform_tags.py.
CODEFORCES_EMOJI = os.getenv("CODEFORCES_EMOJI") or "<:codeforces:1530820117225672890>"
CSES_EMOJI = os.getenv("CSES_EMOJI") or "<:cses:1530822475691196497>"
EULER_EMOJI = os.getenv("EULER_EMOJI") or "<:projecteuler:1530820117879980144>"

# ---------------- LeetCode Contests ----------------
# Contest forums. 0 disables contest posting entirely — the scheduler exits at
# startup and nothing is posted. Set both to a forum channel id to turn weekly /
# biweekly contest threads back on.
LEETCODE_WEEKLY_FORUM_CHANNEL_ID   = int(os.getenv("LEETCODE_WEEKLY_FORUM_CHANNEL_ID",  "0"))
LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID = int(os.getenv("LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID", "0"))

# ---------------- Solve sweep ----------------
# Every problem solved during a co-working session gets a post + a solution
# comment when that session ends, plus one summary card.
SOLVE_SESSION_ROOMS = [
    int(x) for x in (os.getenv("SOLVE_SESSION_ROOMS") or "").replace(" ", "").split(",") if x
] or [1482589316520739077]
# Sessions shorter than this don't sweep — a drop-in isn't a session.
SOLVE_SESSION_MINUTES = int(os.getenv("SOLVE_SESSION_MINUTES") or "60")
# Two visits closer together than this are one session, so stepping between the
# two rooms (or reconnecting) doesn't split a sitting into sub-hour pieces.
SOLVE_SESSION_GAP_MINUTES = int(os.getenv("SOLVE_SESSION_GAP_MINUTES") or "15")
# Heads the summary card in the recap channel.
SOLVE_SESSION_CARD_TITLE = os.getenv("SOLVE_SESSION_CARD_TITLE") or "#co-working"
# Codeforces handle for the public user.status feed. Same name as everywhere else.
CODEFORCES_HANDLE = os.getenv("CODEFORCES_HANDLE") or STREAMER_NAME
# CSES has no public API for solves — the sweep signs in to read them. Unset
# leaves CSES out of the sweep entirely rather than failing it.
CSES_NICK = os.getenv("CSES_NICK") or ""
CSES_PASS = os.getenv("CSES_PASS") or ""
# The zone CSES renders submission times in. It has no per-account setting;
# calibrated 2026-08-26 against a solve with a known session window (only
# Helsinki placed it inside), which is what you'd expect of a Finnish site.
CSES_TZ = os.getenv("CSES_TZ") or "Europe/Helsinki"

# ---------------- #info card ----------------
# The pinned info embed carries a "usually around <time>" Discord timestamp.
# bot/infocard.py re-stamps it daily so it keeps meaning this wall-clock hour
# in this zone across DST, rather than drifting as a fixed instant would.
# "channel:message" pairs carrying the timestamp; one card today, in #info.
INFO_CARDS = [
    tuple(int(x) for x in pair.split(":"))
    for pair in (os.getenv("INFO_CARDS")
                 or "1400587372026003536:1541907284622450740").split(",") if pair.strip()
]
COWORK_USUAL_HOUR = int(os.getenv("COWORK_USUAL_HOUR") or "22")
COWORK_TZ = os.getenv("COWORK_TZ") or "America/Chicago"

# ---------------- Recap ----------------
RECAP_SECRET = os.getenv("RECAP_SECRET", "")
LEETCODE_RECAP_CHANNEL_ID = 1472427491896332490

# ---------------- Stream alerts ----------------
# Go-live announcement (replaces Sapphire's): an embed with the title and game,
# edited into a VOD card when the stream ends. Posted by the twitch bot through
# /stream-alert. Test mode targets #testing.
STREAM_ALERT_CHANNEL_ID = int(os.getenv("STREAM_ALERT_CHANNEL_ID") or "1400572056067641445")
STREAM_ALERT_TEST_CHANNEL_ID = int(os.getenv("STREAM_ALERT_TEST_CHANNEL_ID") or "1541895984403972147")
TWITCH_CHANNEL_URL = f"https://twitch.tv/{STREAMER_NAME}"
# Optional line of content above the embed. Empty leaves the embed alone,
# which is the default — the post carries no mention of any kind.
STREAM_ALERT_TEXT = os.getenv("STREAM_ALERT_TEXT", "")

# ---------------- Join message ----------------
# OFF: joins are Discord's own "Good to see you, X." with its wave sticker
# button, enabled via the guild's system channel (#general) and its
# SUPPRESS_JOIN_NOTIFICATION* flags — not something the bot posts.
#
# Set a channel id to bring ours back instead. It posts WELCOME_TEXT with a
# wave button counting on its label, once per person ever (the `welcomed`
# table); claiming a row up front is how someone is exempted.
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID") or "0")
WELCOME_TEXT = os.getenv("WELCOME_TEXT") or "{mention} just joined."
# The wave button. Its icon is an EMOJI — Discord buttons can't carry a sticker.
# The click count rides on the label, since a bot can't react for someone else.
WELCOME_BUTTON_LABEL = os.getenv("WELCOME_BUTTON_LABEL") or "Wave to say hi!"
WELCOME_BUTTON_EMOJI = os.getenv("WELCOME_BUTTON_EMOJI") or "<:hi:1544773008609116282>"
# A sticker to post per wave. Buttons can't DISPLAY a sticker — Discord allows
# only an emoji there — so this costs a message per wave, which is the whole
# trade against the count-on-the-label version. 0 keeps the count instead.
WELCOME_STICKER_ID = int(os.getenv("WELCOME_STICKER_ID") or "0")

# ---------------- Twitch bot console (outbound control API) ----------------
# Shared secret with the Twitch bot; must match its CONSOLE_SECRET. Never logged.
CONSOLE_SECRET = os.getenv("CONSOLE_SECRET", "")
# Base URL of the Twitch bot's inbound HTTP control API (mirrors its DISCORD_BOT_URL).
TWITCH_BOT_URL = (os.getenv("TWITCH_BOT_URL") or "http://127.0.0.1:8788").rstrip("/")
# The one channel where /twitch console commands are accepted (0 = disabled).
TWITCH_CONSOLE_CHANNEL_ID = int(os.getenv("TWITCH_CONSOLE_CHANNEL_ID") or "0")

# ---------------- Voice Chat Overlay ----------------
VOICECHAT_SECRET = os.getenv("VOICECHAT_SECRET", "")

# ---------------- Secret Streams ----------------
# The room's name is no longer managed: it only reverts if someone /renames it,
# same as any other voice channel. Kept as an id for the attendance rooms below.
SECRET_STREAMS_CHANNEL_ID = 1409455382564180009

# ---------------- Error/Failure Log + Bot Console (mods-only #discord-bot-console) ----------------
DISCORD_LOG_CHANNEL_ID = 1516295491753607268
# Where /alert pings the owner (#twitch-bot-console) — urgent, deliberately
# noisy, unlike the silent log feeds.
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID") or "1516295834046828614")
# Twitch-link approval prompts post to the same mod console channel.
TWITCH_LINK_PROMPT_CHANNEL_ID = DISCORD_LOG_CHANNEL_ID

# ---------------- Voice time ----------------
# Every user's time in every voice channel is recorded (bot/voicetime.py); this
# channel holds the one pinned card that shows the host's share of it.
ADMIN_PANEL_CHANNEL_ID = int(os.getenv("ADMIN_PANEL_CHANNEL_ID") or "1529992719697449143")
# The one room /name can rename. Still the retired #co-working 👥 — /name
# answers "isn't set up" once that channel is deleted, until this is repointed.
VOICE_NAME_CHANNEL_ID = int(os.getenv("VOICE_NAME_CHANNEL_ID") or "1529599559167246548")
# Rooms the host's card totals: #co-working 👂, where the work happens. An
# explicit list rather than "every voice channel", so #general's weekly
# hangout and a room added later don't silently count as work.
VOICE_TIME_HOST_ROOMS = [
    int(x) for x in (os.getenv("VOICE_TIME_HOST_ROOMS") or "").replace(" ", "").split(",") if x
] or [1482589316520739077]
