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
# Built from STREAMER_NAME: the account was renamed from "howlongfantods" and
# the two drifted apart unnoticed, because the api answers a dead handle with
# 200 and an empty list — the recap saw "no submissions", never an error.
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

# ---------------- Nightly solve sweep ----------------
# Every problem solved in the trailing window gets a post + a solution comment,
# fired Tue-Sat (Mon=0) at this hour, server local time. The window is 12h so a
# 05:00 run covers the previous evening and night in one piece.
SOLVE_SWEEP_HOUR = int(os.getenv("SOLVE_SWEEP_HOUR") or "5")
SOLVE_SWEEP_WINDOW_HOURS = int(os.getenv("SOLVE_SWEEP_WINDOW_HOURS") or "12")
SOLVE_SWEEP_DAYS = {int(x) for x in
                    (os.getenv("SOLVE_SWEEP_DAYS") or "1,2,3,4,5").split(",") if x.strip()}
# Codeforces handle for the public user.status feed. Same name as everywhere else.
CODEFORCES_HANDLE = os.getenv("CODEFORCES_HANDLE") or STREAMER_NAME
# CSES has no public API for solves — the sweep signs in to read them. Unset
# leaves CSES out of the sweep entirely rather than failing it.
CSES_NICK = os.getenv("CSES_NICK") or ""
CSES_PASS = os.getenv("CSES_PASS") or ""

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
# The one room /name can rename (#chillin). 0 disables the command's effect.
VOICE_NAME_CHANNEL_ID = int(os.getenv("VOICE_NAME_CHANNEL_ID") or "1529599559167246548")
# The host gets their own card instead, totalling their time in these rooms:
# #general, #co-working, #chillin. An explicit list rather than "everything but
# #on-stream", so a voice channel added later doesn't silently start counting.
VOICE_TIME_HOST_ROOMS = [
    int(x) for x in (os.getenv("VOICE_TIME_HOST_ROOMS") or "").replace(" ", "").split(",") if x
] or [1409455382564180009, 1482589316520739077, 1529599559167246548]
# "Regular" is decided by lifetime time in ONE room (#co-working): past this
# many minutes there, someone is taken to have found their footing and the 1:1
# room is hidden from them indefinitely, keeping it for people who haven't.
# Superseded the old per-session tally, which cooled people down for a single
# long visit regardless of whether they were new.
FAIRACCESS_REGULAR_ROOM = int(os.getenv("FAIRACCESS_REGULAR_ROOM") or "1482589316520739077")
FAIRACCESS_REGULAR_MINUTES = int(os.getenv("FAIRACCESS_REGULAR_MINUTES") or "500")
# When the lifetime rule went live (2026-07-30 14:22 CDT). Only cooldowns from
# at or after this count as "already handled" — everything before it was
# applied by the superseded per-session rule and then bulk-released, and would
# otherwise permanently exempt exactly the regulars this rule is meant to catch.
FAIRACCESS_REGULAR_RULE_SINCE = int(os.getenv("FAIRACCESS_REGULAR_RULE_SINCE") or "1785439368")
# Never auto-cooled, whatever their total: the host runs the room.
FAIRACCESS_EXEMPT_IDS = [
    int(x) for x in (os.getenv("FAIRACCESS_EXEMPT_IDS") or "").replace(" ", "").split(",") if x
] or [SPOTIFY_ALLOWED_USER_ID]
# The tally window resets once all tracked rooms have been empty this long.
FAIRACCESS_WINDOW_RESET_HOURS = float(os.getenv("FAIRACCESS_WINDOW_RESET_HOURS") or "2")
# Members with this role (and the server owner) are exempt from tallying. 0 = owner only.
FAIRACCESS_MOD_ROLE_ID = int(os.getenv("FAIRACCESS_MOD_ROLE_ID") or "0")
