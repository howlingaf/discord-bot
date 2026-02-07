from bot import bot
from bot.config import TOKEN

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(TOKEN)
