import asyncio
import time

import discord
from aiohttp import ClientSession

from .client import bot
from .config import GUILD_ID, LEETCODE_STATUS_CHANNEL_ID, LEETCODE_STATUS_API_URL
from .database import leetcode_status_get, leetcode_status_set


# statusClass -> (emoji, color, label)
STATUS_MAP = {
    "success": ("🟢", 0x00b8a3, "Operational"),
    "warning": ("🟡", 0xffc01e, "Degraded"),
    "danger":  ("🔴", 0xff375f, "Outage"),
}


def _overall_status(monitors: list[dict]) -> str:
    classes = {m["statusClass"] for m in monitors}
    if "danger" in classes:
        return "danger"
    if "warning" in classes:
        return "warning"
    return "success"


async def fetch_status(session: ClientSession) -> list[dict]:
    async with session.get(LEETCODE_STATUS_API_URL) as resp:
        js = await resp.json(content_type=None)
        if resp.status != 200:
            raise RuntimeError(f"Status API failed: {resp.status}")
        return js["data"]


def build_status_embed(monitors: list[dict], checked_at: int) -> discord.Embed:
    overall = _overall_status(monitors)
    emoji, color, label = STATUS_MAP.get(overall, ("⚪", 0x808080, "Unknown"))

    service_lines = []
    for m in monitors:
        m_emoji, _, m_label = STATUS_MAP.get(m["statusClass"], ("⚪", 0x808080, "Unknown"))
        service_lines.append(f"{m_emoji} **{m['name']}** — {m_label}")

    description = (
        "-# Services\n\n"
        + "\n\n".join(service_lines)
        + f"\n\n🕐 Last checked: <t:{checked_at}:R>"
    )

    embed = discord.Embed(
        title=f"{emoji} LeetCode — {label}",
        url="https://status.leetcode.com",
        description=description,
        color=color,
    )
    return embed


async def leetcode_status_scheduler(bot):
    await bot.wait_until_ready()
    await asyncio.sleep(3)
    print("✅ LeetCode status scheduler started (polling every 5 min)")

    while not bot.is_closed():
        try:
            await _run_status_check(bot)
        except Exception as e:
            print(f"[STATUS] error: {e}")
        await asyncio.sleep(300)


async def _run_status_check(bot):
    if not bot.http_session:
        return

    channel = bot.get_channel(LEETCODE_STATUS_CHANNEL_ID) or await bot.fetch_channel(LEETCODE_STATUS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print("[STATUS] status channel is not a text channel")
        return

    monitors = await fetch_status(bot.http_session)
    overall = _overall_status(monitors)
    checked_at = int(time.time())
    embed = build_status_embed(monitors, checked_at)

    message_id, last_status = leetcode_status_get()

    # Update channel name if it doesn't match expected
    emoji, _, label = STATUS_MAP.get(overall, ("⚪", 0x808080, "Unknown"))
    new_channel_name = f"{emoji}・status-{label.lower()}"
    if channel.name != new_channel_name:
        try:
            await channel.edit(name=new_channel_name)
            print(f"[STATUS] channel renamed to {new_channel_name}")
        except Exception as e:
            print(f"[STATUS] channel rename failed: {e}")
    if overall != last_status:
        leetcode_status_set(last_status=overall)

    # Edit existing message or post a new one
    if message_id:
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            pass  # Message was deleted, fall through to post a new one

    msg = await channel.send(embed=embed)
    leetcode_status_set(message_id=msg.id, last_status=overall)
