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

LEETCODE_SUBMISSIONS_URL = "https://leetcode-api-pied.vercel.app/user/howlingfantods_/submissions"
STREAMER_NAME = "howlingfantods_"

# ---------------- LeetCode Problems Forum ----------------
LEETCODE_PROBLEMS_CHANNEL_ID = 1472231552607064144
LEETCODE_DAILY_NOTIF_CHANNEL_ID = 1472396200409043086

# ---------------- LeetCode Contests ----------------
LEETCODE_WEEKLY_CHANNEL_ID = 1470261383701594153
LEETCODE_BIWEEKLY_CHANNEL_ID = 1470261431483105535
LEETCODE_CONTEST_URL = "https://leetcode-api-pied.vercel.app/contests"
LEETCODE_WEEKLY_FORUM_CHANNEL_ID   = int(os.getenv("LEETCODE_WEEKLY_FORUM_CHANNEL_ID",  "1474259972941418496"))
LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID = int(os.getenv("LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID", "1474260036900360193"))

# ---------------- Recap ----------------
RECAP_SECRET = os.getenv("RECAP_SECRET", "")
LEETCODE_RECAP_CHANNEL_ID = 1472427491896332490

# ---------------- Command Logging ----------------
COMMAND_LOG_CHANNEL_ID = 1473840278497525872

# ---------------- LeetCode Premium Weekly ----------------
LEETCODE_PREMIUM_WEEKLY_CHANNEL_ID = 1473828703334174894

# ---------------- LeetCode Status ----------------
LEETCODE_STATUS_CHANNEL_ID = 1473605778030985247
LEETCODE_STATUS_API_URL = "https://status.leetcode.com/api/getMonitorList/yJB2mF66QP"
