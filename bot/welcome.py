"""A join line in chat, with a wave reaction, in our own words.

Discord's built-in welcome can't be reworded and its "Wave to say hi" button
is hardcoded to the Wumpus sticker. This posts our own line and seeds
WELCOME_REACTION on it, so waving is one click straight on the message.

A reaction rather than a button: a bot can only add a reaction as ITSELF, so a
button would leave the count stuck at one however many people clicked. Seeding
it lets each member add their own, which is what makes the count mean anything.
"""

import discord

from .config import WELCOME_CHANNEL_ID, WELCOME_REACTION, WELCOME_TEXT
from .logbus import log_error


def render(member) -> str:
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
        msg = await channel.send(
            render(member), allowed_mentions=discord.AllowedMentions(users=True))
    except Exception as e:
        log_error(f"[WELCOME] could not post for {member.id}: {e!r}")
        return False
    if WELCOME_REACTION:
        # Seeding is cosmetic — the line still stands if the emoji is gone.
        try:
            await msg.add_reaction(WELCOME_REACTION)
        except Exception as e:
            log_error(f"[WELCOME] could not seed {WELCOME_REACTION!r}: {e!r}")
    return True


async def on_member_join(bot, member: discord.Member) -> None:
    await post(bot, member)
