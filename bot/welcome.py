"""A join line in chat, with a wave button, in our own words.

Discord's built-in welcome can't be reworded and its "Wave to say hi" button
is hardcoded to the Wumpus sticker. This posts our own line with our own
button; clicking it waves back with WELCOME_STICKER_ID.

Discord buttons carry an emoji, never a sticker — so the button shows
WELCOME_BUTTON_EMOJI and the sticker is what the click POSTS. With no sticker
configured the wave still works, it just posts the emoji.
"""

import re

import discord

from .config import (
    WELCOME_BUTTON_EMOJI,
    WELCOME_BUTTON_LABEL,
    WELCOME_CHANNEL_ID,
    WELCOME_STICKER_ID,
    WELCOME_TEXT,
)
from .database import welcome_wave_add
from .logbus import log_error

# custom_id carries the joiner's id so the wave can name who it's aimed at.
_TEMPLATE = re.compile(r"^welcome:wave:(?P<joiner>\d+)$")


def render(member) -> str:
    """`{mention}` and `{name}` are the only placeholders."""
    return WELCOME_TEXT.format(
        mention=getattr(member, "mention", f"<@{member.id}>"),
        name=getattr(member, "display_name", None) or member.name,
    )


async def _send_wave(interaction: discord.Interaction, joiner_id: int) -> None:
    """Post the wave. Falls back to the emoji if the sticker can't be sent."""
    waver = interaction.user.mention
    text = f"{waver} waved at <@{joiner_id}>"
    mentions = discord.AllowedMentions(users=False)  # a wave shouldn't ping
    if WELCOME_STICKER_ID and interaction.guild:
        try:
            sticker = await interaction.guild.fetch_sticker(WELCOME_STICKER_ID)
            await interaction.channel.send(text, stickers=[sticker],
                                           allowed_mentions=mentions)
            return
        except Exception as e:
            log_error(f"[WELCOME] sticker {WELCOME_STICKER_ID} unusable: {e!r}")
    await interaction.channel.send(f"{text} {WELCOME_BUTTON_EMOJI}",
                                   allowed_mentions=mentions)


class WaveButton(discord.ui.DynamicItem[discord.ui.Button], template=_TEMPLATE):
    def __init__(self, joiner_id: int):
        self.joiner_id = joiner_id
        super().__init__(discord.ui.Button(
            label=WELCOME_BUTTON_LABEL,
            emoji=WELCOME_BUTTON_EMOJI,
            style=discord.ButtonStyle.secondary,
            custom_id=f"welcome:wave:{joiner_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["joiner"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id == self.joiner_id:
            await interaction.response.send_message(
                "You can't wave at yourself.", ephemeral=True)
            return
        # One wave each, so the button can't be turned into a spam button.
        if not welcome_wave_add(interaction.message.id, interaction.user.id):
            await interaction.response.send_message(
                "You've already waved.", ephemeral=True)
            return
        await interaction.response.defer()
        await _send_wave(interaction, self.joiner_id)


def view(joiner_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(WaveButton(joiner_id))
    return v


async def post(bot, member) -> bool:
    """Best effort: a welcome that fails must never break the join handler."""
    if not WELCOME_CHANNEL_ID:
        return False
    try:
        channel = (bot.get_channel(WELCOME_CHANNEL_ID)
                   or await bot.fetch_channel(WELCOME_CHANNEL_ID))
        await channel.send(render(member), view=view(member.id),
                           allowed_mentions=discord.AllowedMentions(users=True))
        return True
    except Exception as e:
        log_error(f"[WELCOME] could not post for {member.id}: {e!r}")
        return False


async def on_member_join(bot, member: discord.Member) -> None:
    await post(bot, member)


def register(bot) -> None:
    bot.add_dynamic_items(WaveButton)
