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

# ---------------- LeetCode Daily ----------------
LEETCODE_DAILY_CHANNEL_ID = 1469550906587611260
LEETCODE_DAILY_POLL_SECONDS = int(os.getenv("LEETCODE_DAILY_POLL_SECONDS", "600"))

LEETCODE_DAILY_URL = "https://leetcode-api-pied.vercel.app/daily"
LEETCODE_BASE = "https://leetcode.com"

MAX_EXAMPLES = int(os.getenv("LEETCODE_MAX_EXAMPLES", "3"))
LEETCODE_MAX_ACTIVE_THREADS = int(os.getenv("LEETCODE_MAX_ACTIVE_THREADS", "2"))

# ---------------- LeetCode Contests ----------------
LEETCODE_WEEKLY_CHANNEL_ID = 1470261383701594153
LEETCODE_BIWEEKLY_CHANNEL_ID = 1470261431483105535
LEETCODE_CONTEST_URL = "https://leetcode-api-pied.vercel.app/contests"
LEETCODE_CONTEST_POLL_SECONDS = int(os.getenv("LEETCODE_CONTEST_POLL_SECONDS", "3600"))
LEETCODE_CONTEST_MAX_ACTIVE_THREADS = int(os.getenv("LEETCODE_CONTEST_MAX_ACTIVE_THREADS", "2"))
