"""Run the stream-tag sweep once, now, instead of waiting for the bot's.

Applies marks whose problem post has since been created, and takes the Twitch
tag and VOD line off posts whose VOD Twitch no longer has.

    uv run python scripts/streamwork_sweep.py           # sweep
    uv run python scripts/streamwork_sweep.py --list    # show marks, change nothing
"""

import asyncio
import sys
import time

import discord
from aiohttp import ClientSession

sys.path.insert(0, ".")

from bot.config import TOKEN  # noqa: E402
from bot.database import stream_marks_all  # noqa: E402
from bot.streamwork import sweep  # noqa: E402


def show() -> None:
    marks = stream_marks_all()
    if not marks:
        print("No marks.")
        return
    for m in marks:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["marked_at"]))
        print(f"{m['platform']:11} {m['ref']:22} vod {m['vod_id']:>12} "
              f"+{m['offset_s'] // 3600}h{m['offset_s'] % 3600 // 60:02d}m  {when}  "
              f"{'post ' + str(m['thread_id']) if m['thread_id'] else 'not applied'}")


async def run() -> None:
    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        client.http_session = ClientSession()
        try:
            await sweep(client)
            print("Swept.")
            show()
        finally:
            await client.http_session.close()
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    show() if "--list" in sys.argv else asyncio.run(run())
