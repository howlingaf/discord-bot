import os
import time
import secrets
import sqlite3
import urllib.parse

import discord
from discord import app_commands
from dotenv import load_dotenv
from aiohttp import web, ClientSession

load_dotenv()

# ---------------- Env ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Linked Role that Discord grants after user completes Server -> Linked Roles -> Verified -> Connect
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))

# Public URL users can open (cloudflare tunnel URL)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

# Web server bind (keep localhost-only when using tunnel)
WEB_BIND_HOST = os.getenv("WEB_BIND_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))

# SQLite
DB_PATH = os.getenv("DB_PATH", "overlay.db")

# Twitch OAuth (reuse your existing Twitch app)
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_REDIRECT_URI = os.getenv("TWITCH_REDIRECT_URI")  # must match Twitch app redirect exactly

VERIFY_FALLBACK_CHANNEL_ID = int(os.getenv("VERIFY_FALLBACK_CHANNEL_ID", "0"))

# ---------------- Discord intents ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # required for member updates + nickname edits


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


# ---------------- Twitch OAuth helpers ----------------
def twitch_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": "",  # no special scopes needed just to call helix/users with user token
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


# ---------------- Bot ----------------
class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: ClientSession | None = None
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self):
        # Validate required env
        missing = []
        for k in ["DISCORD_TOKEN", "GUILD_ID", "VERIFIED_ROLE_ID", "PUBLIC_BASE_URL", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_REDIRECT_URI"]:
            if not os.getenv(k):
                missing.append(k)
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

        db_init()
        self.http_session = ClientSession()

        # --- Start aiohttp server ---
        app = self._make_web_app()
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, host=WEB_BIND_HOST, port=WEB_PORT)
        await site.start()
        print(f"✅ Verify web server running on http://{WEB_BIND_HOST}:{WEB_PORT}")

        # --- Sync commands ---
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

            # Rename in guild
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


async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    print("---- MEMBER UPDATE EVENT ----")
    print("Member:", after, after.id)

    before_roles = {r.id for r in before.roles}
    after_roles = {r.id for r in after.roles}

    print("Before roles:", before_roles)
    print("After roles :", after_roles)

    added = after_roles - before_roles
    removed = before_roles - after_roles

    print("Added roles:", added)
    print("Removed roles:", removed)

    if VERIFIED_ROLE_ID not in added:
        print("Verified role NOT added — ignoring")
        return

    print("✅ Verified role was added!")

    if has_mapping(after.id):
        print("User already verified — skipping DM")
        return

    print("Attempting to DM user...")

    try:
        await dm_verify_link(after)
        print("✅ DM sent successfully")
        return

    except discord.Forbidden:
        print("❌ DM FAILED — Forbidden")

    except Exception as e:
        print("❌ DM FAILED — Exception:", repr(e))

    print("Attempting fallback channel message...")

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
        await channel.send(
            f"{after.mention} verify your Twitch account here:\n{url}"
        )
        print("✅ Fallback channel message sent")

    except Exception as e:
        print("❌ Failed to post in fallback channel:", repr(e))



# ---- Existing command (manual fallback) ----
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


# ---- New command: /verify fallback (if DMs are closed) ----
@bot.tree.command(name="verify", description="Get the Twitch verify link (fallback if you didn’t receive a DM).")
async def verify(interaction: discord.Interaction):
    if has_mapping(interaction.user.id):
        await interaction.response.send_message("✅ You’re already verified.", ephemeral=True)
        return

    state = create_state(interaction.user.id)
    url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
    await interaction.response.send_message(f"Click to verify your Twitch name:\n{url}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(TOKEN)


