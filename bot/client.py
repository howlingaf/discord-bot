import os

import discord
from aiohttp import ClientSession, web
from discord import app_commands

from .config import GUILD_ID, WEB_BIND_HOST, WEB_PORT
from .database import db_init
from .web import make_web_app

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True


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

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = MyBot()
