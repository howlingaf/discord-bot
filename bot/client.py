import os

import discord
from aiohttp import ClientSession, ClientTimeout, web
from discord import app_commands

from .config import GUILD_ID, WEB_BIND_HOST, WEB_PORT
from .database import db_init
from .web import make_web_app

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.messages = True
intents.message_content = True


class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: ClientSession | None = None
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self):
        missing = []
        required = ["DISCORD_TOKEN", "GUILD_ID", "PUBLIC_BASE_URL"]
        for k in required:
            if not os.getenv(k):
                missing.append(k)
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

        db_init()
        # Default timeout so a hung upstream (LeetCode/pied/GitHub/Spotify) can't
        # stall the serial schedulers indefinitely. discord.py uses its own
        # session for the gateway/REST, so this only affects app HTTP calls.
        self.http_session = ClientSession(timeout=ClientTimeout(total=30))

        self.tree.on_error = self._on_app_command_error

        app = make_web_app(self)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, host=WEB_BIND_HOST, port=WEB_PORT)
        await site.start()
        print(f"\u2705 Verify web server running on http://{WEB_BIND_HOST}:{WEB_PORT}")

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"\u2705 Synced commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("\u2705 Synced commands globally (can take a while to appear)")

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        # Without a global handler, a failed permission check (or any error raised
        # before the body responds) leaves the interaction unanswered — the user
        # just sees "This interaction failed". Always send something ephemeral.
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You don't have permission to use this command."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ You can't use this command here."
        else:
            msg = f"❌ Something went wrong: {error}"
            print(f"[APP-CMD ERROR] {error!r}")
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = MyBot()
