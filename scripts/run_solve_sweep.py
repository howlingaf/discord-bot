"""Run the solve sweep once, now, over a plain time window.

The sweep normally fires when a co-working session ends. This is the manual
path: useful when the bot was down as a session finished, or to sweep a window
that never corresponded to one. It posts problems and comments but no summary
card — the card belongs to a session. Posts for real — there is no dry mode, because the
sweep's own dedup table is what makes a re-run safe: anything already posted is
skipped, so running this twice is not the same as posting twice.

Run from the repo root:
    uv run python scripts/run_solve_sweep.py           # default 12h window
    uv run python scripts/run_solve_sweep.py --hours 36
"""

import asyncio
import sys

import aiohttp
import discord

sys.path.insert(0, ".")

from bot.config import TOKEN  # noqa: E402
from bot.database import db_init  # noqa: E402
from bot.solvesweep import run_sweep  # noqa: E402

HOURS = None
if "--hours" in sys.argv:
    HOURS = int(sys.argv[sys.argv.index("--hours") + 1])


class Runner(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30))

    async def on_ready(self):
        try:
            print(await run_sweep(self, window_hours=HOURS))
        finally:
            if self.http_session:
                await self.http_session.close()
            await self.close()


def main():
    db_init()
    Runner().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
