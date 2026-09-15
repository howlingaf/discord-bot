"""Guest links: someone in the #on-stream call without joining the server.

For interviews and drop-ins where asking a person to join the server would be
pressure. `/guest` makes a single-use invite. Clicking it drops them straight
into the call, and leaving the call removes them from the server.

How they get straight in: the invite carries the Verified role (Discord's
invite `role_ids`), applied the moment it's accepted. Discord only auto-joins
someone into a voice channel from an invite if they're already allowed in when
they click, so the role has to come with the invite — a role or permission the
bot added afterwards would land a split second too late.

Why not Discord's "temporary membership": it removes people when they log off
Discord, not when they leave the call, and any role makes them permanent.

How a guest is recognised: Discord doesn't say which invite a new member used.
A single-use invite is deleted the moment it's used, so a guest link that has
vanished when someone joins is theirs — but only if it was on the server at the
previous tick, or a link revoked by hand earlier would pin the next ordinary
member as a guest. If two vanished at once, the newest is taken; links are made
by hand, one at a time.

The failure that matters: a guest the bot doesn't recognise keeps Verified for
good, because the invite gave it and nothing takes it back. So a guest link
that disappears with no arrival matched to it is reported to the error feed.

State lives in the guest_invites table. Voice events act immediately; a short
loop re-checks every active guest against the call as a backstop, so removal
survives a restart or a missed event.
"""

import asyncio
import time

import discord
from discord.http import Route

from .config import (
    GUEST_CHANNEL_ID,
    GUEST_GRACE_SECONDS,
    GUEST_NO_SHOW_SECONDS,
    GUEST_ROLE_ID,
    GUILD_ID,
)
from .database import (
    guest_active,
    guest_invite_create,
    guest_invites_expired,
    guest_invites_unused,
    guest_update,
)
from .logbus import log_error, log_if_persistent

_TICK_SECONDS = 20
# Roles a guest has without that meaning "keep them": the autorole. The role
# the invite itself hands out is excluded by id in _kept.
_AUTOROLE_NAMES = {"member"}

_lock = asyncio.Lock()
# Invite codes live on the server as of the last tick (None until the first).
_seen_codes: set[str] | None = None


def _now() -> int:
    return int(time.time())


async def create_link(bot, created_by: int, note: str | None, minutes: int) -> tuple[str, int]:
    """Make a single-use invite into the guest channel that grants the guest
    role on acceptance. Returns (url, expires_at).

    Raw route: role_ids is newer than discord.py's create_invite signature.
    """
    reason = f"Guest link for {note}" if note else "Guest link"
    data = await bot.http.request(
        Route("POST", "/channels/{channel_id}/invites", channel_id=GUEST_CHANNEL_ID),
        # Single use, so the link is gone the moment someone arrives on it; the
        # short max_age retires it if nobody does. Neither limits how long a
        # guest who's in can stay — an invite only governs getting in.
        json={"max_uses": 1, "max_age": minutes * 60, "unique": True,
              "role_ids": [str(GUEST_ROLE_ID)]},
        reason=reason)
    if str(GUEST_ROLE_ID) not in {r["id"] for r in data.get("roles", [])}:
        # Without the role the auto-join can't happen; don't hand out a link
        # that quietly doesn't do what it says.
        await bot.http.request(Route("DELETE", "/invites/{code}", code=data["code"]))
        raise RuntimeError("Discord didn't attach the guest role to the invite")
    now = _now()
    guest_invite_create(data["code"], note, created_by, now, now + minutes * 60)
    return f"https://discord.gg/{data['code']}", now + minutes * 60


def _just_used(code: str, live: dict[str, int]) -> bool:
    """Spent by this join: still listed with its one use counted, or gone from
    the list despite being there at the last tick. Before the first tick
    there's no "before" to compare, so only a visible use count will do."""
    if code in live:
        return live[code] >= 1
    return _seen_codes is not None and code in _seen_codes


async def on_member_join(bot, member: discord.Member) -> None:
    """Match a new member to the guest link they arrived on, if any. Never raises."""
    try:
        if member.guild.id != GUILD_ID or member.bot:
            return
        async with _lock:
            unused = guest_invites_unused(_now())
            if not unused:
                return
            # The join event can arrive before Discord deletes the spent
            # invite, so one short retry covers the gap before giving up.
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
            now = _now()
            vc = member.voice.channel if member.voice else None
            guest_update(used["code"], user_id=member.id, joined_at=now,
                         entered_at=now if vc and vc.id == GUEST_CHANNEL_ID else None)
        note = f" ({used['note']})" if used["note"] else ""
        print(f"[GUESTS] {member} arrived on guest link {used['code']}{note}")
    except Exception as e:
        log_error(f"[GUESTS] join handling failed for {member.id}: {e!r}")


def _kept(member: discord.Member) -> bool:
    """A role staff gave them — beyond the autorole and the guest role — means
    they're staying."""
    return any(not r.is_default() and r.id != GUEST_ROLE_ID
               and r.name.lower() not in _AUTOROLE_NAMES for r in member.roles)


async def _end(bot, guild, g: dict, outcome: str) -> None:
    member = guild.get_member(g["user_id"])
    if outcome == "removed" and member is not None:
        try:
            # Leaving the server takes the invite's role with it.
            await member.kick(reason="Guest visit ended")
        except discord.NotFound:
            pass
    guest_update(g["code"], ended_at=_now(), outcome=outcome)
    print(f"[GUESTS] {g['user_id']} guest visit ended: {outcome}")


async def on_voice_state(bot, member: discord.Member,
                         before: discord.VoiceState, after: discord.VoiceState) -> None:
    """Record a guest entering the call; remove them the moment they leave it.
    Never raises."""
    try:
        if member.bot:
            return
        b = before.channel.id if before.channel else None
        a = after.channel.id if after.channel else None
        if b == a or GUEST_CHANNEL_ID not in (a, b):
            return
        async with _lock:
            g = next((x for x in guest_active() if x["user_id"] == member.id), None)
            if g is None or _kept(member):
                return
            if a == GUEST_CHANNEL_ID:
                if g["entered_at"] is None or g["left_at"] is not None:
                    guest_update(g["code"], entered_at=g["entered_at"] or _now(), left_at=None)
            elif GUEST_GRACE_SECONDS <= 0:
                await _end(bot, member.guild, g, "removed")
            else:
                guest_update(g["code"], left_at=_now())   # the tick finishes it
    except Exception as e:
        log_error(f"[GUESTS] voice handling failed for {member.id}: {e!r}")


async def _tick(bot) -> None:
    global _seen_codes
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    live = {i.code for i in await guild.invites()}
    for g in guest_invites_expired(_now()):
        guest_update(g["code"], ended_at=_now(), outcome="expired")
    # An unused link missing for two ticks running wasn't matched to anyone.
    # Usually it was revoked by hand — but if it was used while the bot missed
    # it, that person kept Verified, so say so rather than retire it silently.
    if _seen_codes is not None:
        for g in guest_invites_unused(_now()):
            if g["code"] not in live and g["code"] not in _seen_codes:
                guest_update(g["code"], ended_at=_now(), outcome="unmatched")
                note = f" ({g['note']})" if g["note"] else ""
                log_error(f"[GUESTS] guest link {g['code']}{note} disappeared with no arrival "
                          "matched to it. Fine if it was revoked; if someone used it, they "
                          "still have Verified — check recent joins.")
    _seen_codes = live

    channel = guild.get_channel(GUEST_CHANNEL_ID)
    in_call = {m.id for m in getattr(channel, "members", [])}
    now = _now()
    for g in guest_active():
        member = guild.get_member(g["user_id"])
        if member is None:
            guest_update(g["code"], ended_at=now, outcome="left")  # left on their own
            continue
        if _kept(member):
            guest_update(g["code"], ended_at=now, outcome="kept")
            continue
        if member.id in in_call:
            if g["entered_at"] is None or g["left_at"] is not None:
                guest_update(g["code"], entered_at=g["entered_at"] or now, left_at=None)
            continue
        if g["entered_at"] is not None:
            if g["left_at"] is None:
                guest_update(g["code"], left_at=now)
                if GUEST_GRACE_SECONDS <= 0:
                    await _end(bot, guild, g, "removed")
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
