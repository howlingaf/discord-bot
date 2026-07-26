"""Fair-access cooldown system for the tracked voice rooms.

Two (config-driven) voice rooms are open to everyone, but a user who accrues
FAIRACCESS_THRESHOLD_MINUTES cumulative minutes across all tracked rooms within
one session window gets both rooms hidden from them for FAIRACCESS_COOLDOWN_DAYS.
Everything is silent — no DMs, no announcements; staff see and manage all of it
from the pinned panel in the admin channel.

Design contract:
  * Flag on exit only — accrual and the threshold check happen when a user
    leaves a tracked room, never while they're connected.
  * The session window is global: once ALL tracked rooms have been empty for
    FAIRACCESS_WINDOW_RESET_HOURS, every open tally closes and the next visit
    starts from zero. Bouncing in/out within a window keeps accruing.
  * Enforcement is a per-user permission overwrite on each tracked room denying
    ViewChannel + Connect — no roles, ever. Overwrites are applied/removed via
    raw HTTP so departed members and unresolvable ids work the same.
  * The database is the source of truth. A sweep runs at startup and every
    ~5 minutes: it expires due cooldowns, and reconciles each room's actual
    overwrites against the active records (re-applying missing ones, deleting
    orphans). Only overwrites exactly matching our deny signature are ever
    touched, so unrelated manual per-user overwrites survive.
  * Exemptions: the server owner + FAIRACCESS_MOD_ROLE_ID are invisible to the
    system entirely. Whitelisted users (pure DB state, no Discord artifact) are
    tallied — their sessions show in the visitor feed — but never flagged;
    removing one from the whitelist closes their open tally so exempt minutes
    can't flag them.
  * The admin panel is one bot-owned pinned message, re-rendered wholesale from
    state on every change and at startup — purely informational; all staff
    actions go through the /whitelist and /cooldown slash commands.
"""

import asyncio
import json
import time

import discord

from .config import (
    FAIRACCESS_ADMIN_CHANNEL_ID,
    FAIRACCESS_COOLDOWN_DAYS,
    FAIRACCESS_ENFORCED_ROOMS,
    FAIRACCESS_MOD_ROLE_ID,
    FAIRACCESS_THRESHOLD_MINUTES,
    FAIRACCESS_TRACKED_ROOMS,
    FAIRACCESS_VERIFIED_ROLE_ID,
    FAIRACCESS_WINDOW_RESET_HOURS,
    GUILD_ID,
    STREAMER_DISCORD_ID,
    VOICE_TIME_EXCLUDE_IDS,
    VOICE_TIME_HOST_ROOMS,
    VOICE_TIME_ROOMS,
)
from .database import (
    heartbeat_get,
    heartbeat_set,
    voice_time_by_channel,
    voice_time_totals,
    voice_visit_close,
    voice_visit_open_for,
    voice_visit_recent_same_channel,
    voice_visit_resume,
    voice_visit_start,
    voice_visits_open,
    fairaccess_cooldown_active_for,
    fairaccess_cooldown_create,
    fairaccess_cooldown_mark_expired,
    fairaccess_cooldown_release,
    fairaccess_cooldown_set_expiry,
    fairaccess_cooldowns_active,
    fairaccess_cooldowns_due,
    fairaccess_set_all_empty_since,
    fairaccess_set_panel_message,
    fairaccess_state_get,
    fairaccess_whitelist_add,
    fairaccess_whitelist_all,
    fairaccess_whitelist_has,
    fairaccess_whitelist_remove,
    fairaccess_window_create,
    fairaccess_window_open_for,
    fairaccess_window_update,
    fairaccess_windows_open,
)
from .logbus import log_error, log_if_persistent

_SWEEP_SECONDS = 300
_FEED_LIMIT = 15
# rejoining the same voice channel within this gap resumes the visit row
_VISIT_MERGE_SECONDS = 300
# component budget on the panel message: 5 rows x 5 buttons

# the exact overwrite we own: deny ViewChannel+Connect, allow nothing, member type
_DENY_VALUE = discord.Permissions(view_channel=True, connect=True).value

# Serialize state read-modify-write across voice events, sweeps, and commands.
_lock = asyncio.Lock()


def _now() -> int:
    return int(time.time())


# ------------------------------------------------------------------ #
#  Small helpers                                                     #
# ------------------------------------------------------------------ #

def _is_staff_exempt(member: discord.Member) -> bool:
    """Owner + mod role: invisible to the system entirely."""
    if member.guild.owner_id == member.id:
        return True
    return bool(FAIRACCESS_MOD_ROLE_ID) and any(
        r.id == FAIRACCESS_MOD_ROLE_ID for r in member.roles)


def _room_name(bot, channel_id: int) -> str:
    ch = bot.get_channel(channel_id)
    return ch.name if ch else str(channel_id)


def _fmt_minutes(bot, room_seconds: dict) -> str:
    """'34 min (1:1 22 + streams 12)' from a {channel_id: seconds} dict."""
    total = sum(room_seconds.values()) // 60
    parts = [f"{_room_name(bot, int(cid))} {secs // 60}"
             for cid, secs in sorted(room_seconds.items()) if secs >= 60]
    if len(parts) > 1:
        return f"{total} min ({' + '.join(parts)})"
    return f"{total} min"


def _display_name(bot, user_id: int) -> str:
    guild = bot.get_guild(GUILD_ID)
    m = guild.get_member(user_id) if guild else None
    return m.display_name if m else str(user_id)


async def _admin_channel(bot):
    return bot.get_channel(FAIRACCESS_ADMIN_CHANNEL_ID) or await bot.fetch_channel(FAIRACCESS_ADMIN_CHANNEL_ID)


# ------------------------------------------------------------------ #
#  Overwrites (raw HTTP: works for departed/unresolvable members)    #
# ------------------------------------------------------------------ #

async def _apply_overwrite(bot, channel_id: int, user_id: int) -> bool:
    """Returns False when the user is no longer a guild member (Discord rejects
    member overwrites for non-members; someone who left can't see rooms anyway)."""
    try:
        await bot.http.edit_channel_permissions(
            channel_id, user_id, "0", str(_DENY_VALUE), 1, reason="fair-access cooldown")
        return True
    except discord.NotFound:
        print(f"[FAIRACCESS] skip overwrite for {user_id}: not a guild member")
        return False


async def _remove_overwrite(bot, channel_id: int, user_id: int) -> None:
    try:
        await bot.http.delete_channel_permissions(
            channel_id, user_id, reason="fair-access cooldown ended")
    except discord.NotFound:
        pass


async def _apply_all(bot, user_id: int) -> None:
    for cid in FAIRACCESS_ENFORCED_ROOMS:
        try:
            await _apply_overwrite(bot, cid, user_id)
        except Exception as e:
            log_error(f"[FAIRACCESS] apply overwrite failed ({cid}/{user_id}): {e!r}")


async def _remove_all(bot, user_id: int) -> None:
    for cid in FAIRACCESS_ENFORCED_ROOMS:
        try:
            await _remove_overwrite(bot, cid, user_id)
        except Exception as e:
            log_error(f"[FAIRACCESS] remove overwrite failed ({cid}/{user_id}): {e!r}")


def cooldown_overwrite_targets(channel) -> set[int]:
    """User ids on `channel` holding exactly our cooldown overwrite.

    Only our signature (member-type, allow nothing, deny ViewChannel+Connect)
    matches, so staff's unrelated per-user overwrites are never touched.
    """
    out = set()
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            continue
        if isinstance(target, discord.Object) and getattr(target, "type", None) is discord.Role:
            continue
        allow, deny = ow.pair()
        if allow.value == 0 and deny.value == _DENY_VALUE:
            out.add(target.id)
    return out


# ------------------------------------------------------------------ #
#  Occupancy / session window                                        #
# ------------------------------------------------------------------ #

def _rooms_occupied(bot) -> bool:
    for cid in FAIRACCESS_TRACKED_ROOMS:
        ch = bot.get_channel(cid)
        if ch is not None and any(not m.bot for m in getattr(ch, "members", [])):
            return True
    return False


def _update_occupancy(bot, now: int) -> None:
    st = fairaccess_state_get()
    if _rooms_occupied(bot):
        if st["all_empty_since"] is not None:
            fairaccess_set_all_empty_since(None)
    elif st["all_empty_since"] is None:
        fairaccess_set_all_empty_since(now)


def _close_window(w: dict, now: int, status: str | None = None) -> None:
    """Finalize a window row; status defaults by live whitelist membership."""
    if status is None:
        status = "exempt" if fairaccess_whitelist_has(w["user_id"]) else "ok"
    fairaccess_window_update(w["id"], status=status, last_join_at=None,
                             last_join_channel_id=None, last_activity_at=w["last_activity_at"])


def _reset_stale_windows(now: int) -> bool:
    """Close every open window if all rooms have been empty past the reset gap."""
    st = fairaccess_state_get()
    if st["all_empty_since"] is None:
        return False
    if now - st["all_empty_since"] < FAIRACCESS_WINDOW_RESET_HOURS * 3600:
        return False
    changed = False
    for w in fairaccess_windows_open():
        _close_window(w, now)
        changed = True
    return changed


# ------------------------------------------------------------------ #
#  Voice event handling                                              #
# ------------------------------------------------------------------ #

async def on_voice_state(bot, member: discord.Member,
                         before: discord.VoiceState, after: discord.VoiceState) -> None:
    """Entry point from events.py. Never raises."""
    try:
        if member.bot:
            return
        b = before.channel.id if before.channel else None
        a = after.channel.id if after.channel else None
        if b == a:
            return  # mute/deafen/stream toggles fire this event too
        tracked = set(FAIRACCESS_TRACKED_ROOMS)
        left = b if b in tracked else None
        joined = a if a in tracked else None
        async with _lock:
            now = _now()
            changed = _visit_transition(member.id, b, a, now)
            if left is not None or joined is not None:
                changed |= await _handle_voice(bot, member, left, joined)
        if changed:
            await render_panel(bot)
    except Exception as e:
        log_error(f"[FAIRACCESS] voice handler failed: {e!r}")


def _visit_transition(user_id: int, left_cid: int | None,
                      joined_cid: int | None, now: int) -> bool:
    """Presence log for EVERY voice channel, everyone (staff included).

    One row per user per channel stint, so time can be attributed to the room
    it was actually spent in — the cards aggregate across rows in SQL. Moving
    channels closes one row and opens another; a rejoin to the same channel
    within the merge gap resumes that row instead of fragmenting it.
    """
    changed = False
    if left_cid is not None:
        v = voice_visit_open_for(user_id)
        if v and v["channel_id"] == left_cid:
            voice_visit_close(v["id"], now)
            changed = True
    if joined_cid is not None:
        v = voice_visit_open_for(user_id)
        if v is None:
            merge = voice_visit_recent_same_channel(
                user_id, joined_cid, now - _VISIT_MERGE_SECONDS)
            if merge is not None:
                voice_visit_resume(merge, now)
            else:
                voice_visit_start(user_id, joined_cid, now)
            changed = True
    return changed


async def _handle_voice(bot, member, left_cid: int | None, joined_cid: int | None) -> bool:
    now = _now()
    staff = _is_staff_exempt(member)
    changed = False

    if not staff and left_cid is not None:
        changed |= await _handle_leave(bot, member, left_cid, now)
    if not staff and joined_cid is not None:
        changed |= _handle_join(member, joined_cid, now)

    # occupancy counts everyone (a room with only staff in it is not empty)
    _update_occupancy(bot, now)
    return changed


def _handle_join(member, channel_id: int, now: int) -> bool:
    # a long quiet period means whatever tallies are open belong to a past
    # session — close them all before stamping the new join
    _reset_stale_windows(now)

    w = fairaccess_window_open_for(member.id)
    if w is None:
        wid = fairaccess_window_create(member.id, now)
    else:
        wid = w["id"]
    fairaccess_window_update(wid, last_join_at=now, last_join_channel_id=channel_id,
                             last_activity_at=now)
    return True


async def _handle_leave(bot, member, channel_id: int, now: int) -> bool:
    w = fairaccess_window_open_for(member.id)
    if not w or not w["last_join_at"] or w["last_join_channel_id"] != channel_id:
        return False

    rooms = json.loads(w["room_seconds"])
    key = str(channel_id)
    rooms[key] = rooms.get(key, 0) + max(0, now - w["last_join_at"])
    fairaccess_window_update(w["id"], room_seconds=json.dumps(rooms),
                            last_join_at=None, last_join_channel_id=None,
                            last_activity_at=now)

    total = sum(rooms.values())
    if (total >= FAIRACCESS_THRESHOLD_MINUTES * 60
            and not fairaccess_whitelist_has(member.id)
            and not fairaccess_cooldown_active_for(member.id)):
        await _flag(bot, member.id, rooms, now, window_id=w["id"])
    return True


async def _flag(bot, user_id: int, rooms: dict, now: int,
                window_id: int | None, days: int | None = None,
                applied_by: int | None = None) -> None:
    expires = now + (days or FAIRACCESS_COOLDOWN_DAYS) * 86400
    fairaccess_cooldown_create(user_id, now, expires, json.dumps(rooms), applied_by)
    if window_id is not None:
        fairaccess_window_update(window_id, status="flagged")
    await _apply_all(bot, user_id)


# ------------------------------------------------------------------ #
#  Staff actions (shared by buttons and slash commands)              #
# ------------------------------------------------------------------ #

async def whitelist_add(bot, user_id: int, added_by: int) -> tuple[bool, str]:
    async with _lock:
        if not fairaccess_whitelist_add(user_id, added_by):
            return False, "Already whitelisted."
    await render_panel(bot)   # silent: the list row appearing is the signal
    return True, f"Whitelisted <@{user_id}>."


async def whitelist_remove(bot, user_id: int, removed_by: int) -> tuple[bool, str]:
    async with _lock:
        if not fairaccess_whitelist_remove(user_id):
            return False, "Not on the whitelist."
        # zero their tally: minutes accrued while exempt must not flag them
        w = fairaccess_window_open_for(user_id)
        if w:
            _close_window(w, _now(), status="exempt")
    await render_panel(bot)   # silent: the list row disappearing is the signal
    return True, f"Removed <@{user_id}> from the whitelist (tally reset)."


async def whitelist_seed(bot, seeded_by: int) -> tuple[bool, str]:
    """One-time snapshot of the Verified role's current members. No auto-sync."""
    if not FAIRACCESS_VERIFIED_ROLE_ID:
        return False, "FAIRACCESS_VERIFIED_ROLE_ID isn't configured."
    guild = bot.get_guild(GUILD_ID)
    role = guild.get_role(FAIRACCESS_VERIFIED_ROLE_ID) if guild else None
    if role is None:
        return False, "Verified role not found."
    async with _lock:
        added = sum(1 for m in role.members if fairaccess_whitelist_add(m.id, seeded_by))
    await render_panel(bot)   # silent: the whitelist section shows the result
    return True, f"Seeded {added} member(s) from @{role.name}."


async def session_reset(bot, user_id: int) -> tuple[bool, str]:
    """Zero a user's current session tally. If they're connected right now, a
    fresh window opens immediately so tracking continues from zero."""
    async with _lock:
        w = fairaccess_window_open_for(user_id)
        if not w:
            return False, "No open session tally for that user."
        now = _now()
        rooms = json.loads(w["room_seconds"])
        was_connected = (w["last_join_at"], w["last_join_channel_id"])
        _close_window(w, now)
        if was_connected[0]:
            wid = fairaccess_window_create(user_id, now)
            fairaccess_window_update(wid, last_join_at=now,
                                     last_join_channel_id=was_connected[1],
                                     last_activity_at=now)
    await render_panel(bot)
    return True, f"Reset <@{user_id}>'s tally (was {_fmt_minutes(bot, rooms)})."


async def cooldown_release(bot, user_id: int, released_by: int) -> tuple[bool, str]:
    async with _lock:
        cd = fairaccess_cooldown_active_for(user_id)
        if not cd:
            return False, "No active cooldown for that user."
        fairaccess_cooldown_release(cd["id"], released_by, _now())
    await _remove_all(bot, user_id)
    # no log message: the row disappearing from the panel is the signal
    # (released_by is still recorded on the cooldown row for the audit trail)
    await render_panel(bot)
    return True, f"Released <@{user_id}>."


async def cooldown_reset_all(bot, days: int | None = None) -> tuple[bool, str]:
    """Restart the clock on every active cooldown: each one now expires `days`
    from now, whatever it was set to or how far through it had run."""
    async with _lock:
        actives = fairaccess_cooldowns_active()
        if not actives:
            return False, "No active cooldowns."
        expires = _now() + (days or FAIRACCESS_COOLDOWN_DAYS) * 86400
        for cd in actives:
            fairaccess_cooldown_set_expiry(cd["id"], expires)
    await render_panel(bot)
    return True, (f"Restarted {len(actives)} cooldown"
                  f"{'' if len(actives) == 1 else 's'} — all now release <t:{expires}:R>.")


async def cooldown_apply(bot, user_id: int, applied_by: int,
                         days: int | None = None) -> tuple[bool, str]:
    async with _lock:
        if fairaccess_cooldown_active_for(user_id):
            return False, "That user already has an active cooldown."
        w = fairaccess_window_open_for(user_id)
        rooms = json.loads(w["room_seconds"]) if w else {}
        await _flag(bot, user_id, rooms, _now(),
                    window_id=w["id"] if w else None,
                    days=days, applied_by=applied_by)
    await render_panel(bot)
    return True, f"Cooldown applied to <@{user_id}>."


# ------------------------------------------------------------------ #
#  Admin panel (one pinned message, rebuilt wholesale from state)    #
# ------------------------------------------------------------------ #

_render_lock = asyncio.Lock()


def _build_panel(bot) -> discord.ui.LayoutView:
    """Components-V2 layout, three informational sections; no interactive
    components (staff actions are slash commands)."""
    wl = fairaccess_whitelist_all()
    actives = fairaccess_cooldowns_active()
    totals = voice_time_totals(VOICE_TIME_ROOMS, _now(), _FEED_LIMIT,
                               exclude_user_ids=VOICE_TIME_EXCLUDE_IDS)

    view = discord.ui.LayoutView(timeout=None)

    # ---- whitelist (text only; managed via /whitelist add|remove) ----
    # Two equal-width columns: monospace code block with padded display names
    # (mention pills can't be aligned). Width tracks the longest current name,
    # clamped at 24 (Discord names cap at 32; most are far shorter).
    shown = wl[:40]
    names = []
    for r in shown:
        n = _display_name(bot, r["user_id"])
        names.append(n[:23] + "…" if len(n) > 24 else n)
    width = max((len(n) for n in names), default=0)
    wl_lines = [names[i].ljust(width) + "  " + names[i + 1] if i + 1 < len(names)
                else names[i]
                for i in range(0, len(names), 2)]
    body = ("```\n" + "\n".join(wl_lines) + "\n```") if wl_lines else "*empty*"
    if len(wl) > 40:
        body += f"\n-# …and {len(wl) - 40} more"
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("### Fair access — whitelist"),
        discord.ui.TextDisplay(body + "\n-# manage via `/whitelist add` · `/whitelist remove`"),
        accent_color=0x43B581))

    # ---- active cooldowns (text only; released via /cooldown release) ----
    cd_body = "\n".join(f"<@{c['user_id']}> · releases <t:{c['expires_at']}:R>"
                        for c in actives) or "*none*"
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("### Active cooldowns"),
        discord.ui.TextDisplay(cd_body + "\n-# release via `/cooldown release`"),
        accent_color=0xED4245))

    # ---- attendance: total time per member across the logged rooms ----
    feed_lines = [f"<@{t['user_id']}> · {t['seconds'] // 60} min" for t in totals]
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("### Discord Stream Attendance"),
        discord.ui.TextDisplay("\n".join(feed_lines) or "*no time logged yet*"),
        accent_color=0x4E5058))

    # ---- the host's own time across their rooms ----
    if STREAMER_DISCORD_ID:
        rooms = voice_time_by_channel(STREAMER_DISCORD_ID, _now(), VOICE_TIME_HOST_ROOMS)
        total = sum(r["seconds"] for r in rooms) // 60
        body = [f"**{total // 60}h {total % 60}m** total"]
        body += [f"#{_room_name(bot, r['channel_id'])} · {r['seconds'] // 60} min"
                 for r in rooms if r["seconds"] >= 60]
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"### <@{STREAMER_DISCORD_ID}>'s voice time"),
            discord.ui.TextDisplay("\n".join(body) if rooms else "*no time logged yet*"),
            discord.ui.TextDisplay(
                "-# " + " · ".join(f"#{_room_name(bot, c)}" for c in VOICE_TIME_HOST_ROOMS)),
            accent_color=0xFAA61A))

    return view


async def render_panel(bot) -> None:
    """Re-render the pinned panel wholesale; recreate it once if deleted (or if
    the stored message predates the layout format and can't be edited into it)."""
    async with _render_lock:
        try:
            channel = await _admin_channel(bot)
            view = _build_panel(bot)
            mid = fairaccess_state_get()["panel_message_id"]
            if mid:
                try:
                    await channel.get_partial_message(mid).edit(view=view)
                    return
                except discord.NotFound:
                    pass  # deleted — recreate below (once)
                except discord.HTTPException:
                    # legacy embed message can't convert to components-v2 in place
                    try:
                        await channel.get_partial_message(mid).delete()
                    except Exception:
                        pass
            msg = await channel.send(view=view)
            fairaccess_set_panel_message(msg.id)
            try:
                await msg.pin()
            except Exception as e:
                print(f"[FAIRACCESS] could not pin panel: {e}")
        except Exception as e:
            log_error(f"[FAIRACCESS] panel render failed: {e!r}")


# ------------------------------------------------------------------ #
#  Sweep: expiries + reconciliation                                  #
# ------------------------------------------------------------------ #

async def _sweep(bot) -> bool:
    """Expire due cooldowns, settle stale windows, reconcile overwrites."""
    now = _now()
    heartbeat_set(now)
    changed = False

    for cd in fairaccess_cooldowns_due():
        fairaccess_cooldown_mark_expired(cd["id"], now)
        await _remove_all(bot, cd["user_id"])
        changed = True   # silent: the panel row disappearing is the signal

    changed |= _reset_stale_windows(now)

    # reconcile: DB is truth. Re-apply missing overwrites; drop orphans that
    # exactly match our signature.
    active_ids = {c["user_id"] for c in fairaccess_cooldowns_active()}
    for cid in FAIRACCESS_ENFORCED_ROOMS:
        ch = bot.get_channel(cid)
        if ch is None:
            continue
        existing = cooldown_overwrite_targets(ch)
        for uid in active_ids - existing:
            await _apply_overwrite(bot, cid, uid)
            print(f"[FAIRACCESS] reconciled: re-applied overwrite for {uid} on #{ch.name}")
        for uid in existing - active_ids:
            await _remove_overwrite(bot, cid, uid)
            print(f"[FAIRACCESS] reconciled: removed orphan overwrite for {uid} on #{ch.name}")

    return changed


def _startup_fixups(bot, now: int) -> None:
    """A restart must never reset a tally — but a join that was open when the
    bot went down can't be credited if the user left during the downtime (we
    never saw the leave). If they're still connected, the stamp stays valid and
    the whole span (downtime included) counts; otherwise the segment is lost."""
    for w in fairaccess_windows_open():
        if not w["last_join_at"]:
            continue
        ch = bot.get_channel(w["last_join_channel_id"] or 0)
        still_there = ch is not None and any(
            m.id == w["user_id"] for m in getattr(ch, "members", []))
        if not still_there:
            fairaccess_window_update(w["id"], last_join_at=None, last_join_channel_id=None)
            print(f"[FAIRACCESS] cleared dangling join for {w['user_id']} (left during downtime)")
    # Attendance sessions: a restart must not cost anyone the time they were
    # sitting on. If they're still in a room the row stays open and the whole
    # span counts; otherwise credit them up to the last heartbeat — the last
    # moment we know they were connected — rather than dropping the segment.
    beat = heartbeat_get()
    for v in voice_visits_open():
        # rows are per channel, so the row is still live only if the user is
        # still in that exact channel
        ch = bot.get_channel(v["channel_id"])
        still_there = ch is not None and any(
            m.id == v["user_id"] for m in getattr(ch, "members", []))
        if not still_there:
            credit_to = max(v["last_join_at"], min(beat or now, now))
            voice_visit_close(v["id"], credit_to)


async def _loop(bot) -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(5)

    async with _lock:
        _startup_fixups(bot, _now())
        _update_occupancy(bot, _now())
        try:
            await _sweep(bot)
        except Exception as e:
            log_error(f"[FAIRACCESS] startup sweep failed: {e!r}")
    await render_panel(bot)
    print(f"✅ Fair-access system started (rooms: {FAIRACCESS_TRACKED_ROOMS}, "
          f"sweep every {_SWEEP_SECONDS}s)")

    fails = 0
    while not bot.is_closed():
        await asyncio.sleep(_SWEEP_SECONDS)
        try:
            async with _lock:
                changed = await _sweep(bot)
            if changed:
                await render_panel(bot)
            fails = 0
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[FAIRACCESS] sweep failed (attempt {fails}): {e!r}")


def start(bot) -> None:
    """Register components and launch the sweep loop once (idempotent)."""
    if getattr(bot, "_fairaccess_started", False):
        return
    bot._fairaccess_started = True
    bot.loop.create_task(_loop(bot))
