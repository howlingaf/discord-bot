"""The "Sounds good" gate: one button in #info that grants the Member role.

New joins see only #info (everything else is hidden from @everyone); clicking
the button grants Member, whose base permissions include View Channels, and
the server appears. Existing members were never gated — everyone already held
Member when this went in — so the button only ever adds, never removes.

Restart-safe the same way the Twitch-link controls are: a DynamicItem matched
by custom_id, registered once at startup, so the button on the reposted card
keeps working across every deploy with no message edits.
"""

import re

import discord

from .config import MEMBER_ROLE_ID
from .logbus import log_error

CUSTOM_ID = "gate:member"
_TEMPLATE = re.compile(r"^gate:member$")


class SoundsGoodButton(discord.ui.DynamicItem[discord.ui.Button], template=_TEMPLATE):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Sounds good", emoji="✅",
            style=discord.ButtonStyle.success, custom_id=CUSTOM_ID))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Run this inside the server.", ephemeral=True)
            return
        role = member.guild.get_role(MEMBER_ROLE_ID)
        if role is None:
            log_error(f"[GATE] MEMBER_ROLE_ID {MEMBER_ROLE_ID} not found in guild")
            await interaction.response.send_message(
                "Something's off on our end — ping a mod.", ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("You're already in.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Accepted #info via Sounds good")
        except discord.Forbidden:
            log_error(f"[GATE] cannot assign {role.name}: bot role must sit above it")
            await interaction.response.send_message(
                "Something's off on our end — ping a mod.", ephemeral=True)
            return
        await interaction.response.send_message("You're in — welcome!", ephemeral=True)


def view() -> discord.ui.View:
    """A view carrying the button, for posting the card."""
    v = discord.ui.View(timeout=None)
    v.add_item(SoundsGoodButton())
    return v


def register(bot) -> None:
    bot.add_dynamic_items(SoundsGoodButton)
