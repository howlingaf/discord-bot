"""Locking #on-stream to new arrivals, shared by /lock, /unlock, and the
Twitch bot's review-lock endpoint (a Track/Album Review redemption locks the
channel while the music plays).

Only Connect changes: people already in the call stay, and everyone can
still see who's in it. The owner is an admin and gets in regardless; mods
get Connect of their own while it's locked, and it's taken back on unlock
so nothing lingers.
"""

import discord

from .config import GUEST_CHANNEL_ID, GUEST_ROLE_ID, MODERATOR_ROLE_ID
from .logbus import log_error


def _with_connect(channel, role, value):
    ow = channel.overwrites_for(role)
    ow.connect = value
    return ow


async def set_on_stream_lock(guild, locked: bool, reason: str) -> tuple[bool, str]:
    """Apply the lock state. Idempotent: already-there counts as success."""
    channel = guild.get_channel(GUEST_CHANNEL_ID)
    verified, mods = guild.get_role(GUEST_ROLE_ID), guild.get_role(MODERATOR_ROLE_ID)
    if channel is None or verified is None or mods is None:
        return False, "Couldn't find #on-stream or its roles."
    if (channel.overwrites_for(verified).connect is False) == locked:
        return True, f"already {'locked' if locked else 'unlocked'}"
    try:
        # Mods first on lock and last on unlock, so they're never shut out
        # between the two.
        if locked:
            await channel.set_permissions(
                mods, overwrite=_with_connect(channel, mods, True), reason=reason)
        await channel.set_permissions(
            verified, overwrite=_with_connect(channel, verified, not locked),
            reason=reason)
        if not locked:
            await channel.set_permissions(
                mods, overwrite=_with_connect(channel, mods, None), reason=reason)
    except discord.HTTPException as e:
        log_error(f"[voicelock {reason}] {e!r}")
        return False, f"Couldn't change it: {e}"
    return True, "locked" if locked else "unlocked"
