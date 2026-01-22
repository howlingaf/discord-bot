import os
import secrets
import sqlite3
import time
import urllib.parse

import discord
from aiohttp import ClientSession, web
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ---------------- Env ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

WEB_BIND_HOST = os.getenv("WEB_BIND_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))

DB_PATH = os.getenv("DB_PATH", "overlay.db")

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_REDIRECT_URI = os.getenv("TWITCH_REDIRECT_URI")

VERIFY_FALLBACK_CHANNEL_ID = int(os.getenv("VERIFY_FALLBACK_CHANNEL_ID", "0"))

# ---------------- Spotify env ----------------
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_ALLOWED_USER_ID = int(os.getenv("SPOTIFY_ALLOWED_USER_ID", "0"))  # your discord user id
SPOTIFY_VOICE_CHANNEL_ID = int(os.getenv("SPOTIFY_VOICE_CHANNEL_ID", "0"))
SPOTIFY_PAUSE_THRESHOLD = int(os.getenv("SPOTIFY_PAUSE_THRESHOLD", "2"))
SPOTIFY_DEBOUNCE_SECONDS = int(os.getenv("SPOTIFY_DEBOUNCE_SECONDS", "2"))

SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state"


# ---------------- Discord intents ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True  # <-- needed for voiceStateUpdate events


# ---------------- SQLite helpers ----------------
def _db():
    return sqlite3.connect(DB_PATH)


def db_init():
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS twitch_map (
          discord_user_id INTEGER PRIMARY KEY,
          twitch_display_name TEXT NOT NULL,
          twitch_login TEXT,
          twitch_user_id TEXT,
          updated_at INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS verify_state (
          state TEXT PRIMARY KEY,
          discord_user_id INTEGER NOT NULL,
          expires_at INTEGER NOT NULL
        )
        """)

        # Spotify tokens for ONE account (yours)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_tokens (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          access_token TEXT,
          refresh_token TEXT,
          expires_at INTEGER,
          updated_at INTEGER NOT NULL
        )
        """)
        # remember if bot paused (so we only resume our own pauses)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_runtime (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          paused_by_bot INTEGER NOT NULL DEFAULT 0,
          last_action_at INTEGER NOT NULL DEFAULT 0,
          last_member_count INTEGER NOT NULL DEFAULT -1
        )
        """)
        conn.execute("INSERT OR IGNORE INTO spotify_runtime(id, paused_by_bot, last_action_at, last_member_count) VALUES(1,0,0,-1)")
        conn.commit()


def has_mapping(discord_user_id: int) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM twitch_map WHERE discord_user_id=?",
            (discord_user_id,),
        ).fetchone()
        return row is not None


def upsert_mapping(discord_user_id: int, display_name: str, login: str, twitch_user_id: str):
    now = int(time.time())
    with _db() as conn:
        conn.execute("""
        INSERT INTO twitch_map(discord_user_id, twitch_display_name, twitch_login, twitch_user_id, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(discord_user_id) DO UPDATE SET
          twitch_display_name=excluded.twitch_display_name,
          twitch_login=excluded.twitch_login,
          twitch_user_id=excluded.twitch_user_id,
          updated_at=excluded.updated_at
        """, (discord_user_id, display_name, login, twitch_user_id, now))
        conn.commit()


def create_state(discord_user_id: int, ttl_sec: int = 15 * 60) -> str:
    state = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + ttl_sec
    with _db() as conn:
        conn.execute(
            "INSERT INTO verify_state(state, discord_user_id, expires_at) VALUES(?,?,?)",
            (state, discord_user_id, expires_at),
        )
        conn.commit()
    return state


def consume_state(state: str) -> int | None:
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT discord_user_id, expires_at FROM verify_state WHERE state=?",
            (state,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM verify_state WHERE state=?", (state,))
        conn.commit()

    discord_user_id, expires_at = int(row[0]), int(row[1])
    return discord_user_id if expires_at >= now else None


# ---------------- Spotify DB helpers ----------------
def spotify_get_tokens() -> tuple[str | None, str | None, int | None]:
    with _db() as conn:
        row = conn.execute("SELECT access_token, refresh_token, expires_at FROM spotify_tokens WHERE id=1").fetchone()
        if not row:
            return None, None, None
        return row[0], row[1], row[2]


def spotify_upsert_tokens(access_token: str, refresh_token: str | None, expires_in: int):
    now = int(time.time())
    expires_at = now + int(expires_in) - 15  # 15s safety buffer
    with _db() as conn:
        # keep existing refresh_token if spotify doesn't return one
        existing = conn.execute("SELECT refresh_token FROM spotify_tokens WHERE id=1").fetchone()
        existing_rt = existing[0] if existing else None
        rt = refresh_token or existing_rt

        conn.execute("""
        INSERT INTO spotify_tokens(id, access_token, refresh_token, expires_at, updated_at)
        VALUES(1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          access_token=excluded.access_token,
          refresh_token=excluded.refresh_token,
          expires_at=excluded.expires_at,
          updated_at=excluded.updated_at
        """, (access_token, rt, expires_at, now))
        conn.commit()


def spotify_get_runtime() -> tuple[bool, int, int]:
    with _db() as conn:
        row = conn.execute("SELECT paused_by_bot, last_action_at, last_member_count FROM spotify_runtime WHERE id=1").fetchone()
        paused_by_bot = bool(row[0])
        return paused_by_bot, int(row[1]), int(row[2])


def spotify_set_runtime(*, paused_by_bot: bool | None = None, last_action_at: int | None = None, last_member_count: int | None = None):
    with _db() as conn:
        if paused_by_bot is not None:
            conn.execute("UPDATE spotify_runtime SET paused_by_bot=? WHERE id=1", (1 if paused_by_bot else 0,))
        if last_action_at is not None:
            conn.execute("UPDATE spotify_runtime SET last_action_at=? WHERE id=1", (int(last_action_at),))
        if last_member_count is not None:
            conn.execute("UPDATE spotify_runtime SET last_member_count=? WHERE id=1", (int(last_member_count),))
        conn.commit()


# ---------------- Twitch OAuth helpers ----------------
def twitch_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": "",
        "state": state,
    })
    return f"https://id.twitch.tv/oauth2/authorize?{qs}"


async def twitch_exchange_code(session: ClientSession, code: str) -> dict:
    token_url = "https://id.twitch.tv/oauth2/token"
    data = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TWITCH_REDIRECT_URI,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Twitch token exchange failed: {js}")
        return js


async def twitch_get_user(session: ClientSession, access_token: str) -> dict:
    url = "https://api.twitch.tv/helix/users"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Client-Id": TWITCH_CLIENT_ID,
    }
    async with session.get(url, headers=headers) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Twitch get user failed: {js}")
        data = js.get("data", [])
        if not data:
            raise web.HTTPBadRequest(text="No user data returned from Twitch.")
        return data[0]


# ---------------- Spotify OAuth helpers ----------------
def spotify_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "response_type": "code",
        "scope": SPOTIFY_SCOPES,
        "state": state,
        "show_dialog": "true",
    })
    return f"https://accounts.spotify.com/authorize?{qs}"


async def spotify_exchange_code(session: ClientSession, code: str) -> dict:
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Spotify token exchange failed: {js}")
        return js


async def spotify_refresh(session: ClientSession, refresh_token: str) -> dict:
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Spotify refresh failed: {js}")
        return js


async def spotify_get_access_token(session: ClientSession) -> str | None:
    access_token, refresh_token, expires_at = spotify_get_tokens()
    now = int(time.time())

    if not refresh_token:
        return None

    if access_token and expires_at and expires_at > now:
        return access_token

    js = await spotify_refresh(session, refresh_token)
    new_access = js["access_token"]
    new_refresh = js.get("refresh_token")  # may be absent
    expires_in = js.get("expires_in", 3600)
    spotify_upsert_tokens(new_access, new_refresh, expires_in)
    return new_access


async def spotify_api_json(session: ClientSession, method: str, url: str, access_token: str, *, expected=(200,), json_body=None):
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.request(method, url, headers=headers, json=json_body) as resp:
        if resp.status in expected:
            if resp.status == 204:
                return None
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                return await resp.json()
            return None
        try:
            js = await resp.json()
        except Exception:
            js = await resp.text()
        raise web.HTTPBadRequest(text=f"Spotify API error {resp.status}: {js}")


async def spotify_get_playback(session: ClientSession, access_token: str) -> dict | None:
    # returns None if no active device or nothing playing
    url = "https://api.spotify.com/v1/me/player"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status == 204:
            return None
        js = await resp.json()
        if resp.status != 200:
            return None
        return js


async def spotify_pause(session: ClientSession, access_token: str) -> bool:
    url = "https://api.spotify.com/v1/me/player/pause"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.put(url, headers=headers) as resp:
        return resp.status in (204, 202)


async def spotify_play(session: ClientSession, access_token: str) -> bool:
    url = "https://api.spotify.com/v1/me/player/play"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.put(url, headers=headers) as resp:
        return resp.status in (204, 202)


# ---------------- Bot ----------------
class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: ClientSession | None = None
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self):
        missing = []
        required = [
            "DISCORD_TOKEN", "GUILD_ID", "VERIFIED_ROLE_ID", "PUBLIC_BASE_URL",
            "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_REDIRECT_URI",
        ]
        for k in required:
            if not os.getenv(k):
                missing.append(k)
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

        db_init()
        self.http_session = ClientSession()

        app = self._make_web_app()
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, host=WEB_BIND_HOST, port=WEB_PORT)
        await site.start()
        print(f"✅ Verify web server running on http://{WEB_BIND_HOST}:{WEB_PORT}")

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Synced commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("✅ Synced commands globally (can take a while to appear)")

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    def _make_web_app(self) -> web.Application:
        routes = web.RouteTableDef()

        @routes.get("/health")
        async def health(_: web.Request):
            return web.Response(text="ok", content_type="text/plain")

        # ---- Twitch verification ----
        @routes.get("/verify/start")
        async def verify_start(request: web.Request):
            state = request.query.get("state")
            if not state:
                raise web.HTTPBadRequest(text="Missing state")
            return web.HTTPFound(twitch_authorize_url(state))

        @routes.get("/twitch/callback")
        async def twitch_callback(request: web.Request):
            if request.query.get("error"):
                desc = request.query.get("error_description") or "Cancelled."
                return web.Response(text=f"Verification cancelled: {desc}", content_type="text/plain")

            code = request.query.get("code")
            state = request.query.get("state")
            if not code or not state:
                raise web.HTTPBadRequest(text="Missing code/state")

            discord_user_id = consume_state(state)
            if not discord_user_id:
                return web.Response(text="This verify link is invalid or expired. Please try again.", content_type="text/plain")

            session = self.http_session
            if session is None:
                raise web.HTTPServiceUnavailable(text="Bot not ready")

            token_js = await twitch_exchange_code(session, code)
            access_token = token_js["access_token"]

            user = await twitch_get_user(session, access_token)
            twitch_id = user["id"]
            twitch_login = user["login"]
            twitch_display = user["display_name"]

            upsert_mapping(discord_user_id, twitch_display, twitch_login, twitch_id)

            guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
            member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)

            ok, why = await try_set_nick(member, twitch_display)
            if ok:
                return web.Response(text=f"✅ Verified! Nickname set to: {twitch_display}\nYou can close this window.", content_type="text/plain")

            return web.Response(
                text=f"Verified as {twitch_display}, but couldn’t set nickname.\nReason: {why}\n"
                     f"(If you're server owner/admin, Discord blocks bots from renaming you.)",
                content_type="text/plain",
            )

        # ---- Spotify OAuth (for YOU) ----
        @routes.get("/spotify/start")
        async def spotify_start(request: web.Request):
            state = request.query.get("state")
            if not state:
                raise web.HTTPBadRequest(text="Missing state")
            if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI):
                raise web.HTTPBadRequest(text="Spotify env not configured.")
            return web.HTTPFound(spotify_authorize_url(state))

        @routes.get("/spotify/callback")
        async def spotify_callback(request: web.Request):
            if request.query.get("error"):
                desc = request.query.get("error_description") or "Cancelled."
                return web.Response(text=f"Spotify auth cancelled: {desc}", content_type="text/plain")

            code = request.query.get("code")
            state = request.query.get("state")
            if not code or not state:
                raise web.HTTPBadRequest(text="Missing code/state")

            discord_user_id = consume_state(state)
            if not discord_user_id:
                return web.Response(text="This Spotify link is invalid or expired. Please try again.", content_type="text/plain")

            if SPOTIFY_ALLOWED_USER_ID and discord_user_id != SPOTIFY_ALLOWED_USER_ID:
                return web.Response(text="Not allowed to link Spotify for this bot.", content_type="text/plain")

            session = self.http_session
            if session is None:
                raise web.HTTPServiceUnavailable(text="Bot not ready")

            token_js = await spotify_exchange_code(session, code)
            access_token = token_js["access_token"]
            refresh_token = token_js.get("refresh_token")
            expires_in = token_js.get("expires_in", 3600)

            if not refresh_token:
                return web.Response(
                    text="Spotify did not return a refresh_token. Remove bot access in Spotify and try again.\n"
                         "Spotify: Settings → Apps → Remove access, then re-link.",
                    content_type="text/plain",
                )

            spotify_upsert_tokens(access_token, refresh_token, expires_in)
            spotify_set_runtime(paused_by_bot=False, last_action_at=0, last_member_count=-1)

            return web.Response(
                text="✅ Spotify linked! Auto pause/resume can now work.\nYou can close this window.",
                content_type="text/plain",
            )

        app = web.Application()
        app.add_routes(routes)
        return app


bot = MyBot()


async def try_set_nick(member: discord.Member, display_name: str) -> tuple[bool, str]:
    try:
        await member.edit(nick=display_name, reason="Twitch verified: set nickname to Twitch display name")
        return True, "ok"
    except discord.Forbidden:
        return False, "Forbidden (owner/admin or role hierarchy/permission)"
    except discord.HTTPException as e:
        return False, f"HTTPException: {e}"


async def dm_verify_link(member: discord.Member):
    state = create_state(member.id)
    url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
    await member.send(
        "Almost done — click once to confirm your Twitch display name for on-stream voice:\n"
        f"{url}\n\n"
        "After this, I’ll set your server nickname permanently."
    )


async def dm_spotify_link(user: discord.Member):
    state = create_state(user.id)
    url = f"{PUBLIC_BASE_URL}/spotify/start?state={urllib.parse.quote(state)}"
    await user.send(
        "Link Spotify (one-time) so I can auto pause/resume during voice:\n"
        f"{url}"
    )


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    print("---- MEMBER UPDATE EVENT ----")
    print("Member:", after, after.id)

    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}

    added = after_roles - before_roles

    if VERIFIED_ROLE_ID not in added:
        return

    print("✅ Verified role was added!")

    if has_mapping(after.id):
        print("User already verified — skipping DM")
        return

    try:
        await dm_verify_link(after)
        print("✅ DM sent successfully")
        return
    except discord.Forbidden:
        print("❌ DM FAILED — Forbidden")
    except Exception as e:
        print("❌ DM FAILED — Exception:", repr(e))

    if not VERIFY_FALLBACK_CHANNEL_ID:
        print("❌ VERIFY_FALLBACK_CHANNEL_ID not set")
        return

    channel = bot.get_channel(VERIFY_FALLBACK_CHANNEL_ID)
    if not channel:
        print("❌ Could not resolve fallback channel ID:", VERIFY_FALLBACK_CHANNEL_ID)
        return

    try:
        state = create_state(after.id)
        url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
        await channel.send(f"{after.mention} verify your Twitch account here:\n{url}")
        print("✅ Fallback channel message sent")
    except Exception as e:
        print("❌ Failed to post in fallback channel:", repr(e))


def _count_humans_in_channel(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)


async def _handle_spotify_auto_pause(member_count: int):
    """
    member_count is # of non-bot users in the watched voice channel.
    Pause if member_count >= threshold.
    Resume if member_count <= 1 AND bot had paused it.
    """
    if not bot.http_session:
        return

    paused_by_bot, last_action_at, last_member_count = spotify_get_runtime()
    now = int(time.time())

    # debounce + avoid repeated calls for same count
    if member_count == last_member_count and (now - last_action_at) < SPOTIFY_DEBOUNCE_SECONDS:
        return
    if (now - last_action_at) < SPOTIFY_DEBOUNCE_SECONDS:
        # still update member count so we can react after debounce
        spotify_set_runtime(last_member_count=member_count)
        return

    spotify_set_runtime(last_member_count=member_count)

    access = await spotify_get_access_token(bot.http_session)
    if not access:
        # not linked
        return

    threshold = SPOTIFY_PAUSE_THRESHOLD if SPOTIFY_PAUSE_THRESHOLD > 0 else 2

    if member_count >= threshold:
        # only pause if currently playing
        playback = await spotify_get_playback(bot.http_session, access)
        is_playing = bool(playback and playback.get("is_playing"))
        if is_playing:
            ok = await spotify_pause(bot.http_session, access)
            if ok:
                spotify_set_runtime(paused_by_bot=True, last_action_at=now)
        return

    return

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # must be configured
    if not SPOTIFY_VOICE_CHANNEL_ID:
        return

    # If neither before nor after involve the watched channel, ignore
    watched_id = SPOTIFY_VOICE_CHANNEL_ID
    before_id = before.channel.id if before and before.channel else None
    after_id = after.channel.id if after and after.channel else None
    if before_id != watched_id and after_id != watched_id:
        return

    # Resolve watched channel and count humans
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(watched_id)
    if not isinstance(channel, discord.VoiceChannel):
        return

    member_count = _count_humans_in_channel(channel)
    await _handle_spotify_auto_pause(member_count)


# ---- Existing command ----
@bot.tree.command(name="settwitch", description="Set your server nickname to your Twitch display name.")
@app_commands.describe(display_name="Your Twitch display name (e.g., hairyrug_)")
async def settwitch(interaction: discord.Interaction, display_name: str):
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this command inside the server.", ephemeral=True)
        return

    ok, why = await try_set_nick(member, display_name)
    if ok:
        await interaction.response.send_message(f"✅ Set your nickname to **{display_name}**", ephemeral=True)
        return

    await interaction.response.send_message(
        "❌ I can't change your nickname.\n"
        f"Reason: {why}\n\n"
        "Fix:\n"
        "• Give me **Manage Nicknames** permission\n"
        "• Put my bot role **above** your role in **Server Settings → Roles**\n"
        "• Enable **Server Members Intent** in the Developer Portal\n"
        "• Note: bots cannot rename server owners/admins",
        ephemeral=True
    )


# ---- /verify fallback ----
@bot.tree.command(name="verify", description="Get the Twitch verify link (fallback if you didn’t receive a DM).")
async def verify(interaction: discord.Interaction):
    if has_mapping(interaction.user.id):
        await interaction.response.send_message("✅ You’re already verified.", ephemeral=True)
        return

    state = create_state(interaction.user.id)
    url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
    await interaction.response.send_message(f"Click to verify your Twitch name:\n{url}", ephemeral=True)


# ---- /spotifylink (only you) ----
@bot.tree.command(name="spotifylink", description="(Owner) DM yourself the Spotify link so the bot can auto pause/resume.")
async def spotifylink(interaction: discord.Interaction):
    if SPOTIFY_ALLOWED_USER_ID and interaction.user.id != SPOTIFY_ALLOWED_USER_ID:
        await interaction.response.send_message("❌ Not allowed.", ephemeral=True)
        return

    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI):
        await interaction.response.send_message("❌ Spotify env not configured.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this inside the server.", ephemeral=True)
        return

    try:
        await dm_spotify_link(member)
        await interaction.response.send_message("✅ Check your DMs for the Spotify link.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't DM you. Open DMs temporarily or I can post a link in a channel.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(TOKEN)
