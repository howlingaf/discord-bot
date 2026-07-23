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

# NOTE: LeetCode account handle. Still "howlongfantods" until it can be renamed
# to "howlingaf" on 2026-07-07 — update this URL then.
LEETCODE_SUBMISSIONS_URL = "https://leetcode-api-pied.vercel.app/user/howlongfantods/submissions"
STREAMER_NAME = "howlingaf"
# The streamer's Discord id (= the bot owner) so streamer solution lines can show
# a silent @mention instead of the plain name. 0 -> fall back to STREAMER_NAME.
STREAMER_DISCORD_ID = SPOTIFY_ALLOWED_USER_ID

# ---------------- LeetCode Problems Forum ----------------
LEETCODE_PROBLEMS_CHANNEL_ID = 1472231552607064144
LEETCODE_DAILY_NOTIF_CHANNEL_ID = 1472396200409043086

# ---------------- LeetCode Contests ----------------
LEETCODE_WEEKLY_FORUM_CHANNEL_ID   = int(os.getenv("LEETCODE_WEEKLY_FORUM_CHANNEL_ID",  "1474259972941418496"))
LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID = int(os.getenv("LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID", "1474260036900360193"))

# ---------------- Recap ----------------
RECAP_SECRET = os.getenv("RECAP_SECRET", "")
LEETCODE_RECAP_CHANNEL_ID = 1472427491896332490

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
SECRET_STREAMS_CHANNEL_ID = 1409455382564180009
SECRET_STREAMS_EMPTY_NAME = "super secret streams"

# ---------------- Command Logging ----------------
COMMAND_LOG_CHANNEL_ID = 1473840278497525872

# ---------------- Error/Failure Log + Bot Console (mods-only #discord-bot-console) ----------------
DISCORD_LOG_CHANNEL_ID = 1516295491753607268
# Twitch-link approval prompts post to the same mod console channel.
TWITCH_LINK_PROMPT_CHANNEL_ID = DISCORD_LOG_CHANNEL_ID

# ---------------- #schedule (calendar aggregator) ----------------
# The channel that holds the two pinned, edit-in-place schedule messages.
SCHEDULE_CHANNEL_ID = int(os.getenv("SCHEDULE_CHANNEL_ID") or "0")
# All display is anchored to this IANA timezone; timed entries also render in each
# viewer's local time via Discord dynamic timestamps.
SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "America/Chicago")
# Google Calendar API key, restricted to the Calendar API. Public calendars need
# no OAuth. Without it the sync falls back to any per-calendar ICS urls.
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
# Seconds between sync cycles. A calendar edit should surface within ~1 min.
SCHEDULE_POLL_SECONDS = int(os.getenv("SCHEDULE_POLL_SECONDS") or "60")
# JSON file listing the calendars to aggregate (source of truth is config, not
# code). See schedule_calendars.example.json for the shape.
SCHEDULE_CALENDARS_FILE = os.getenv("SCHEDULE_CALENDARS_FILE", "schedule_calendars.json")
# A fully-failed sync cycle alerts privately via log_error() -> #discord-bot-console.

