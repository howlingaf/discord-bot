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
# The room's name is no longer managed: it only reverts if someone /renames it,
# same as any other voice channel. Kept as an id for the attendance rooms below.
SECRET_STREAMS_CHANNEL_ID = 1409455382564180009

# ---------------- Command Logging ----------------
COMMAND_LOG_CHANNEL_ID = 1473840278497525872

# ---------------- Error/Failure Log + Bot Console (mods-only #discord-bot-console) ----------------
DISCORD_LOG_CHANNEL_ID = 1516295491753607268
# Twitch-link approval prompts post to the same mod console channel.
TWITCH_LINK_PROMPT_CHANNEL_ID = DISCORD_LOG_CHANNEL_ID

# ---------------- Fair-access cooldown (tracked voice rooms) ----------------
# Staff-only channel holding the pinned admin panel + append-only action log.
FAIRACCESS_ADMIN_CHANNEL_ID = int(os.getenv("FAIRACCESS_ADMIN_CHANNEL_ID") or "1529992719697449143")
# Voice channels subject to the fair-access rules, comma-separated ids. Default
# is the development/test room; swap in the real 1:1 + streams rooms here.
FAIRACCESS_TRACKED_ROOMS = [
    int(x) for x in (os.getenv("FAIRACCESS_TRACKED_ROOMS") or "1528837173275787415").replace(" ", "").split(",") if x
]
# Rooms a cooldown actually hides (ViewChannel+Connect deny). Defaults to the
# tracked list; set narrower so some rooms accrue time but stay enterable.
FAIRACCESS_ENFORCED_ROOMS = [
    int(x) for x in (os.getenv("FAIRACCESS_ENFORCED_ROOMS") or "").replace(" ", "").split(",") if x
] or list(FAIRACCESS_TRACKED_ROOMS)
# Rooms the attendance card totals time for — and the only rooms the session
# log records. Deliberately separate from FAIRACCESS_TRACKED_ROOMS (which drives
# cooldowns): this is just "how long has each member attended".
# Defaults: #co-working, #1:1 chillin, #on-stream, #super secret streams.
VOICE_TIME_ROOMS = [
    int(x) for x in (os.getenv("VOICE_TIME_ROOMS") or "").replace(" ", "").split(",") if x
] or [1482589316520739077, 1529599559167246548, 1393005093045145631, SECRET_STREAMS_CHANNEL_ID]
# Members left off that list — the host's own hours aren't what the panel is for.
VOICE_TIME_EXCLUDE_IDS = [
    int(x) for x in (os.getenv("VOICE_TIME_EXCLUDE_IDS") or "").replace(" ", "").split(",") if x
] or [1236756328307757157]  # howlingaf
# The host gets their own card instead, totalling their time in these rooms:
# #general, #co-working, #chillin. An explicit list rather than "everything but
# #on-stream", so a voice channel added later doesn't silently start counting.
VOICE_TIME_HOST_ROOMS = [
    int(x) for x in (os.getenv("VOICE_TIME_HOST_ROOMS") or "").replace(" ", "").split(",") if x
] or [1409455382564180009, 1482589316520739077, 1529599559167246548]
# A user accruing this many cumulative minutes across ALL tracked rooms (within
# one session window) is cooled down on their next exit.
FAIRACCESS_THRESHOLD_MINUTES = int(os.getenv("FAIRACCESS_THRESHOLD_MINUTES") or "30")
# The tally window resets once all tracked rooms have been empty this long.
FAIRACCESS_WINDOW_RESET_HOURS = float(os.getenv("FAIRACCESS_WINDOW_RESET_HOURS") or "2")
FAIRACCESS_COOLDOWN_DAYS = int(os.getenv("FAIRACCESS_COOLDOWN_DAYS") or "7")
# Members with this role (and the server owner) are exempt from tallying. 0 = owner only.
FAIRACCESS_MOD_ROLE_ID = int(os.getenv("FAIRACCESS_MOD_ROLE_ID") or "0")
# Role whose current members /whitelist seed imports (one-time snapshot; no auto-sync).
FAIRACCESS_VERIFIED_ROLE_ID = int(os.getenv("FAIRACCESS_VERIFIED_ROLE_ID") or "0")
