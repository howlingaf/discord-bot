"""Guest links: someone in the #on-stream call without joining the server.

For interviews and drop-ins where asking a person to join the server would be
pressure. `/guest` makes a single-use invite; whoever arrives on it gets access
to that one voice channel, and is removed from the server once they leave it.

Why not Discord's own "temporary membership": it removes people when they log
off Discord, not when they leave the call, and any role makes them permanent —
the server's autorole hands everyone Member on join, so it would never fire.

How a guest is recognised: Discord doesn't say which invite a new member used.
A single-use invite is deleted the moment it's used, so a guest link that has
vanished from the server's invite list when someone joins is the one they came
in on. If two vanished at once (someone already in the server used one), the
newest is taken — links are made by hand, one at a time.

Access is a per-person permission on the channel, never a role: a role would
make them a regular member. If staff DO give a guest a real role, that's read
as "keep them" and the bot stops treating them as a guest.

State lives in the guest_invites table, and a short loop re-evaluates every
active guest against who is actually in the channel, so removal survives a
restart and a missed voice event can't strand anyone.
"""

import asyncio
import time

import discord

from .config import (
    GUEST_CHANNEL_ID,
    GUEST_GRACE_SECONDS,
    GUEST_NO_SHOW_SECONDS,
    GUILD_ID,
)
from .database import guest_active, guest_invite_create, guest_invites_unused, guest_update
from .logbus import log_error, log_if_persistent

_TICK_SECONDS = 20
# Roles a guest can have without that meaning "keep them": the autorole.
_AUTOROLE_NAMES = {"member"}
_ACCESS = discord.PermissionOverwrite(
    view_channel=True, connect=True, speak=True, stream=True,
    use_voice_activation=True, send_messages=True, read_message_history=True)

_lock = asyncio.Lock()
# Invite codes live on the server as of the last tick (None until the first).
# A guest link only counts as used by a join if it was here moments before,
# so one revoked by hand earlier isn't mistaken for the next person's way in.
_seen_codes: set[str] | None = None


def _now() -> int:
    return int(time.time())


async def create_link(bot, created_by: int, note: str | None, hours: int) -> tuple[str, int]:
    """Make a single-use invite to the guest channel. Returns (url, expires_at)."""
    channel = bot.get_channel(GUEST_CHANNEL_ID) or await bot.fetch_channel(GUEST_CHANNEL_ID)
    invite = await channel.create_invite(
        max_uses=1, max_age=hours * 3600, unique=True,
        reason=f"Guest link{f' for {note}' if note else ''}")
    now = _now()
    guest_invite_create(invite.code, note, created_by, now, now + hours * 3600)
    return invite.url, now + hours * 3600


async def on_member_join(bot, member: discord.Member) -> None:
    """Match a new member to the guest link they arrived on, if any. Never raises."""
    try:
        if member.guild.id != GUILD_ID or member.bot:
            return
        async with _lock:
            unused = guest_invites_unused(_now())
            if not unused:
                return
            # The join event can arrive before Discord deletes the spent invite,
            # so a use count at its limit counts as used too, and one short
            # retry covers the gap before giving up on this join.
            vanished = []
            for delay in (0, 3):
                await asyncio.sleep(delay)
                live = {i.code: i.uses for i in await member.guild.invites()}
                vanished = [g for g in unused if _just_used(g["code"], live)]
                if vanished:
                    break
            if not vanished:
                return          # joined some other way
            used = vanished[-1]  # newest; see the module docstring
            for stale in vanished[:-1]:
                guest_update(stale["code"], ended_at=_now(), outcome="unclaimed")
            guest_update(used["code"], user_id=member.id, joined_at=_now())
        channel = bot.get_channel(GUEST_CHANNEL_ID) or await bot.fetch_channel(GUEST_CHANNEL_ID)
        await channel.set_permissions(member, overwrite=_ACCESS,
                                      reason="Guest link: access to this channel only")
        note = f" ({used['note']})" if used["note"] else ""
        print(f"[GUESTS] {member} arrived on guest link {used['code']}{note}")
    except Exception as e:
        log_error(f"[GUESTS] join handling failed for {member.id}: {e!r}")


def _just_used(code: str, live: dict[str, int]) -> bool:
    """Spent by this join: still listed with its one use counted, or gone from
    the list despite being there at the last tick. Before the first tick
    there's no "before" to compare, so only a visible use count will do."""
    if code in live:
        return live[code] >= 1
    return _seen_codes is not None and code in _seen_codes


def _kept(member: discord.Member) -> bool:
    """A real role, given by staff, means they're staying."""
    return any(not r.is_default() and r.name.lower() not in _AUTOROLE_NAMES
               for r in member.roles)


async def _end(bot, guild, g: dict, outcome: str) -> None:
    member = guild.get_member(g["user_id"])
    # Raw call: discord.py won't target a user who has already left the server,
    # and a guest who left on their own still has an overwrite to clear.
    try:
        await bot.http.delete_channel_permissions(
            GUEST_CHANNEL_ID, g["user_id"], reason="Guest visit ended")
    except discord.NotFound:
        pass
    if outcome == "removed" and member is not None:
        try:
            await member.kick(reason="Guest visit ended")
        except discord.NotFound:
            pass
    guest_update(g["code"], ended_at=_now(), outcome=outcome)
    print(f"[GUESTS] {g['user_id']} guest visit ended: {outcome}")


async def _tick(bot) -> None:
    global _seen_codes
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    live = {i.code for i in await guild.invites()}
    # An unused link missing for two ticks running wasn't spent on a join —
    # it was revoked. Retire it so it can never be matched to someone later.
    if _seen_codes is not None:
        for g in guest_invites_unused(_now()):
            if g["code"] not in live and g["code"] not in _seen_codes:
                guest_update(g["code"], ended_at=_now(), outcome="revoked")
    _seen_codes = live

    channel = guild.get_channel(GUEST_CHANNEL_ID)
    in_call = {m.id for m in getattr(channel, "members", [])}
    now = _now()
    for g in guest_active():
        member = guild.get_member(g["user_id"])
        if member is None:
            # they left the server themselves
            await _end(bot, guild, g, "left")
            continue
        if _kept(member):
            # staff gave them a real role: no longer a guest, access stays as-is
            guest_update(g["code"], ended_at=now, outcome="kept")
            continue
        if member.id in in_call:
            updates = {}
            if g["entered_at"] is None:
                updates["entered_at"] = now
            if g["left_at"] is not None:
                updates["left_at"] = None      # came back within the grace period
            if updates:
                guest_update(g["code"], **updates)
            continue
        if g["entered_at"] is not None:
            if g["left_at"] is None:
                guest_update(g["code"], left_at=now)
            elif now - g["left_at"] >= GUEST_GRACE_SECONDS:
                await _end(bot, guild, g, "removed")
        elif now - g["joined_at"] >= GUEST_NO_SHOW_SECONDS:
            await _end(bot, guild, g, "removed")


async def _loop(bot) -> None:
    await bot.wait_until_ready()
    fails = 0
    while not bot.is_closed():
        try:
            async with _lock:
                await _tick(bot)
            fails = 0
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[GUESTS] tick failed (attempt {fails}): {e!r}")
        await asyncio.sleep(_TICK_SECONDS)


def start(bot) -> None:
    if getattr(bot, "_guests_started", False):
        return
    bot._guests_started = True
    bot.loop.create_task(_loop(bot))
