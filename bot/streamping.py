"""The "notify me when live" button on the #readme card.

One button, one role: pressing it gives you the Stream Pings role, pressing it
again takes it away, and the go-live post in #streams mentions that role. The
role exists so a go-live ping only reaches people who asked for one -- the
@everyone ping was dropped in September for the opposite reason.

The button is a DynamicItem, so it keeps working across every restart with no
stored message ids or view registry: its custom_id is all the state there is.
"""

import discord

from .config import INFO_CARDS, STREAM_PING_ROLE_ID
from .logbus import log_error

LABEL = "Notify me when live"
_TEMPLATE = r"sp:toggle"


class ToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_TEMPLATE):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label=LABEL, style=discord.ButtonStyle.secondary,
                                           emoji="🔔", custom_id="sp:toggle"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        role = interaction.guild and interaction.guild.get_role(STREAM_PING_ROLE_ID)
        if not role:
            await interaction.response.send_message("That role is missing — tell a mod.", ephemeral=True)
            return
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Stream pings: opted out")
                text = "Done — no more pings when the stream starts."
            else:
                await interaction.user.add_roles(role, reason="Stream pings: opted in")
                text = "You'll get a ping in #streams when the stream goes live. Press again to stop."
        except Exception as e:
            log_error(f"[STREAMPING] toggling for {interaction.user.id} failed: {e!r}")
            text = "That didn't work — tell a mod."
        await interaction.response.send_message(text, ephemeral=True)


def view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ToggleButton())
    return v


def register(bot) -> None:
    bot.add_dynamic_items(ToggleButton)


async def attach(bot) -> None:
    """Put the button on the #readme card, once. Editing only the view leaves
    the embeds alone, so this is safe to re-run."""
    if not STREAM_PING_ROLE_ID:
        return
    for channel_id, message_id in INFO_CARDS:
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            msg = await channel.fetch_message(message_id)
            if not msg.components:
                await msg.edit(view=view())
                print(f"[STREAMPING] button added to {channel_id}/{message_id}")
        except Exception as e:
            log_error(f"[STREAMPING] could not add the button to {channel_id}/{message_id}: {e!r}")
