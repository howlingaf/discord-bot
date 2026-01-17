import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # required for nickname edits

class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Fast command propagation (guild-only sync)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Synced commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("✅ Synced commands globally (can take a while to appear)")

bot = MyBot()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

@bot.tree.command(name="settwitch", description="Set your server nickname to your Twitch display name.")
@app_commands.describe(display_name="Your Twitch display name (e.g., hairyrug_)")
async def settwitch(interaction: discord.Interaction, display_name: str):
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this command inside the server.", ephemeral=True)
        return

    try:
        await member.edit(nick=display_name)
        await interaction.response.send_message(
            f"✅ Set your nickname to **{display_name}**",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I can't change your nickname.\n"
            "Fix:\n"
            "• Give me **Manage Nicknames** permission\n"
            "• Put my bot role **above** your role in **Server Settings → Roles**\n"
            "• Enable **Server Members Intent** in the Developer Portal",
            ephemeral=True
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"❌ Discord error while updating nickname: {e}",
            ephemeral=True
        )

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(TOKEN)
