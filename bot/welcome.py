"""A join line in chat, in our own words.

Discord's built-in welcome message can't be reworded — it picks at random from
a fixed list and prompts a wave sticker — so this posts instead. Point
WELCOME_CHANNEL_ID at a test channel until the wording is settled.
"""

import discord

from .config import WELCOME_CHANNEL_ID, WELCOME_TEXT
from .logbus import log_error


def render(member: discord.Member | discord.User) -> str:
    """`{mention}` and `{name}` are the only placeholders."""
    return WELCOME_TEXT.format(
        mention=getattr(member, "mention", f"<@{member.id}>"),
        name=getattr(member, "display_name", None) or member.name,
    )


async def post(bot, member) -> bool:
    """Best effort: a welcome that fails must never break the join handler."""
    if not WELCOME_CHANNEL_ID:
        return False
    try:
        channel = (bot.get_channel(WELCOME_CHANNEL_ID)
                   or await bot.fetch_channel(WELCOME_CHANNEL_ID))
        # Only the new member is mentionable — no stray role/everyone pings
        # if the wording ever grows one.
        await channel.send(render(member),
                           allowed_mentions=discord.AllowedMentions(users=True))
        return True
    except Exception as e:
        log_error(f"[WELCOME] could not post for {member.id}: {e!r}")
        return False


async def on_member_join(bot, member: discord.Member) -> None:
    await post(bot, member)
