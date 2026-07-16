"""Point the reaction roster at a post.

The bot keeps the roster updated on its own once a post is tracked — this is
only needed to (re)aim it, e.g. after the tracked post is deleted and reposted.

    uv run python scripts/track_reactions.py <message-link-or-id> <emoji>
    uv run python scripts/track_reactions.py --untrack <message-id>

Logs in over HTTP only (no gateway session), so it's safe to run while the bot
is up. Reactions already on the post are picked up; bots are ignored.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discord  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from bot.config import INTEREST_ROSTER_CHANNEL_ID  # noqa: E402
from bot.database import reaction_track_delete  # noqa: E402
from bot.interest import resolve_message, start_tracking  # noqa: E402


async def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)

    if args[0] == "--untrack":
        if len(args) < 2 or not args[1].isdigit():
            print("--untrack needs a message id")
            raise SystemExit(2)
        print("untracked" if reaction_track_delete(int(args[1])) else "wasn't tracked")
        return

    if len(args) < 2:
        print("need <message-link-or-id> <emoji>")
        raise SystemExit(2)
    ref, emoji = args[0], args[1]

    client = discord.Client(intents=discord.Intents.none())
    await client.login(os.environ["DISCORD_TOKEN"])
    try:
        message, err = await resolve_message(client, ref)
        if not message:
            print(f"failed: {err}")
            raise SystemExit(1)
        ok, msg = await start_tracking(client, message, emoji, INTEREST_ROSTER_CHANNEL_ID)
        print(("ok: " if ok else "failed: ") + msg)
        if not ok:
            raise SystemExit(1)
    finally:
        await client.close()


asyncio.run(main())
