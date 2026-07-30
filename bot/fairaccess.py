"""Regulars: who no longer needs the room kept for newcomers.

The enforced room is for people still finding their footing. Anyone whose
LIFETIME time in FAIRACCESS_REGULAR_ROOM (#co-working) passes
FAIRACCESS_REGULAR_MINUTES counts as a regular and has it hidden from them
indefinitely. Everything is silent — no DMs, no announcements; staff see and
manage all of it from the pinned panel in the admin channel.

Design contract:
  * "Regular" is a lifetime total in one room, not a rate. It only ever grows,
    so the check is idempotent: crossing the line marks someone once and
    re-running changes nothing.
  * Someone is marked at most once. Since the total never falls back below the
    line, re-marking would undo any `/regular remove` the moment the next sweep
    ran, leaving `/regular remove` with no lasting effect.
    That "already handled" check starts at FAIRACCESS_REGULAR_RULE_SINCE:
    the rows written by the superseded per-session cooldown rule were bulk
    released, and would otherwise have permanently exempted the very regulars
    this rule exists to catch.
  * The DB still calls a regular a "cooldown" (fairaccess_cooldowns, and the
    fairaccess_cooldown_* accessors). The name is historical — renaming the
    table would buy nothing but a migration.
  * Session windows still record per-visit tallies for the audit trail, but no
    longer decide anything — they drove the superseded per-session rule.
  * Enforcement is a per-user permission overwrite on each tracked room denying
    ViewChannel + Connect — no roles, ever. Overwrites are applied/removed via
    raw HTTP so departed members and unresolvable ids work the same.
  * The database is the source of truth. A sweep runs at startup and every
    ~5 minutes: it expires due cooldowns, and reconciles each room's actual
    overwrites against the active records (re-applying missing ones, deleting
    orphans). Only overwrites exactly matching our deny signature are ever
    touched, so unrelated manual per-user overwrites survive.
  * Exemptions: the server owner + FAIRACCESS_MOD_ROLE_ID are invisible to the
    system entirely, as is FAIRACCESS_EXEMPT_IDS (the host). There is no
    whitelist — it was removed 2026-07-30 as a second, overlapping way to say
    "leave this person alone". `/regular remove` is permanent and covers it.
  * The admin panel is one bot-owned pinned message, re-rendered wholesale from
    state on every change and at startup — purely informational; all staff
    actions go through the /regular slash commands.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta

import discord

from .config import (
    FAIRACCESS_ADMIN_CHANNEL_ID,
    FAIRACCESS_ENFORCED_ROOMS,
    FAIRACCESS_MOD_ROLE_ID,
    FAIRACCESS_REGULAR_ROOM,
    FAIRACCESS_REGULAR_RULE_SINCE,
    FAIRACCESS_REGULAR_MINUTES,
    FAIRACCESS_EXEMPT_IDS,
    FAIRACCESS_TRACKED_ROOMS,
    FAIRACCESS_WINDOW_RESET_HOURS,
    GUILD_ID,
    STREAMER_DISCORD_ID,
    VOICE_TIME_EXCLUDE_IDS,
    VOICE_TIME_HOST_ROOMS,
    VOICE_TIME_ROOMS,
)
from .database import (
    INDEFINITE,
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
    voice_visits_since,
    fairaccess_cooldown_active_for,
    fairaccess_cooldown_create,
    fairaccess_cooldown_ever_for,
    fairaccess_cooldown_mark_expired,
    fairaccess_cooldown_release,
    fairaccess_cooldowns_active,
    fairaccess_cooldowns_due,
    fairaccess_set_all_empty_since,
    fairaccess_set_panel_message,
    fairaccess_state_get,
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
    """Finalize a window row."""
    if status is None:
        status = "ok"
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
            # Leaving the regular room is when its total last moved, so check
            # then rather than making them wait out the sweep interval.
            if b == FAIRACCESS_REGULAR_ROOM:
                changed |= await _apply_regulars(bot)
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

    # No threshold here any more: the tally is kept for the record, but what
    # earns a cooldown is the lifetime total in the regular room, checked by
    # _apply_regulars on the sweep and on leaving that room.
    return True


async def _apply_regulars(bot) -> bool:
    """Cool down anyone whose lifetime regular-room total has crossed the line.

    Runs on the sweep and whenever someone leaves that room. Returns whether
    anything changed, so the caller can re-render the panel.
    """
    if not FAIRACCESS_REGULAR_ROOM:
        return False
    now = _now()
    limit = FAIRACCESS_REGULAR_MINUTES * 60
    changed = False
    # No feed limit here: this decides access, so it has to see every user, not
    # the top slice the panel shows.
    for row in voice_time_totals([FAIRACCESS_REGULAR_ROOM], now, limit=10_000):
        user_id = row["user_id"]
        if (row["seconds"] < limit
                or user_id in FAIRACCESS_EXEMPT_IDS
                or fairaccess_cooldown_ever_for(user_id, FAIRACCESS_REGULAR_RULE_SINCE)):
            continue
        await _flag(bot, user_id, {str(FAIRACCESS_REGULAR_ROOM): row["seconds"]},
                    now, window_id=None)
        print(f"[FAIRACCESS] {user_id} passed {FAIRACCESS_REGULAR_MINUTES}m in "
              f"{FAIRACCESS_REGULAR_ROOM} ({row['seconds'] // 60}m) — cooled down")
        changed = True
    return changed


async def _flag(bot, user_id: int, rooms: dict, now: int,
                window_id: int | None, applied_by: int | None = None,
                expires_at: int = INDEFINITE) -> None:
    """Mark someone a regular. Never dated now — only the superseded rule set
    an expiry, and its rows are what the sweep's expiry pass still drains."""
    fairaccess_cooldown_create(user_id, now, expires_at, json.dumps(rooms), applied_by)
    if window_id is not None:
        fairaccess_window_update(window_id, status="flagged")
    await _apply_all(bot, user_id)


# ------------------------------------------------------------------ #
#  Staff actions (shared by buttons and slash commands)              #
# ------------------------------------------------------------------ #

async def regular_add(bot, user_id: int, added_by: int) -> tuple[bool, str]:
    """Mark someone a regular by hand, without waiting for their total."""
    async with _lock:
        if fairaccess_cooldown_active_for(user_id):
            return False, "Already a regular."
        w = fairaccess_window_open_for(user_id)
        rooms = json.loads(w["room_seconds"]) if w else {}
        await _flag(bot, user_id, rooms, _now(),
                    window_id=w["id"] if w else None,
                    applied_by=added_by)
    await render_panel(bot)
    return True, f"<@{user_id}> is now a regular."


async def regular_remove(bot, user_id: int, removed_by: int) -> tuple[bool, str]:
    """Un-mark a regular, restoring the room. Permanent: the automatic check
    skips anyone already marked once, so this is not re-applied later."""
    async with _lock:
        cd = fairaccess_cooldown_active_for(user_id)
        if not cd:
            return False, "That user isn't a regular."
        fairaccess_cooldown_release(cd["id"], removed_by, _now())
    await _remove_all(bot, user_id)
    # no log message: the row disappearing from the panel is the signal
    # (removed_by is still recorded on the row for the audit trail)
    await render_panel(bot)
    return True, f"<@{user_id}> is no longer a regular."


async def regular_remove_all(bot, removed_by: int) -> tuple[bool, str]:
    """Un-mark every regular at once — regular_remove, applied to each."""
    async with _lock:
        actives = fairaccess_cooldowns_active()
        if not actives:
            return False, "No regulars."
        now = _now()
        for cd in actives:
            fairaccess_cooldown_release(cd["id"], removed_by, now)
    # Overwrite removal is Discord I/O — outside the lock, as in regular_remove.
    for cd in actives:
        await _remove_all(bot, cd["user_id"])
    await render_panel(bot)
    return True, (f"Cleared {len(actives)} regular"
                  f"{'' if len(actives) == 1 else 's'}.")


# ------------------------------------------------------------------ #
#  Admin panel (one pinned message, rebuilt wholesale from state)    #
# ------------------------------------------------------------------ #

_render_lock = asyncio.Lock()


_HOST_WEEKS = 6


def _week_start(ts: int) -> int:
    """Unix time of the Monday 00:00 that opens `ts`'s week, server local time."""
    d = datetime.fromtimestamp(ts)
    monday = (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


def _hm(seconds: int) -> str:
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _weekly_totals(user_id: int, rooms: list[int], now: int,
                   weeks: int = _HOST_WEEKS) -> list[tuple[int, int]]:
    """[(week_start, seconds)] for the last `weeks` weeks, newest first.

    A visit counts toward the week it STARTED in, so one running through
    Sunday midnight lands wholly in the earlier week. Rejoins within the merge
    gap extend their original row, which is what makes that possible at all —
    splitting would mean logging every join separately.

    Trailing empty weeks are dropped: voice logging only began 2026-07-24, so
    the window would otherwise open on rows of 0h that mean "not recorded"
    rather than "wasn't here". This week and last week always survive, so the
    week-over-week comparison is always there to read.
    """
    if not rooms:
        return []
    this_week = _week_start(now)
    starts = [int((datetime.fromtimestamp(this_week) - timedelta(weeks=i)).timestamp())
              for i in range(weeks)]
    buckets = {s: 0 for s in starts}
    for v in voice_visits_since(user_id, rooms, starts[-1]):
        bucket = _week_start(v["started_at"])
        if bucket not in buckets:
            continue
        secs = v["seconds"]
        if v["left_at"] is None:
            secs += max(0, now - v["last_join_at"])
        buckets[bucket] += secs
    out = [(s, buckets[s]) for s in starts]
    while len(out) > 2 and out[-1][1] == 0:
        out.pop()
    return out


def _build_panel(bot) -> discord.ui.LayoutView:
    """Components-V2 layout, informational sections only; no interactive
    components (staff actions are slash commands)."""
    actives = fairaccess_cooldowns_active()
    totals = voice_time_totals(VOICE_TIME_ROOMS, _now(), _FEED_LIMIT,
                               exclude_user_ids=VOICE_TIME_EXCLUDE_IDS)

    view = discord.ui.LayoutView(timeout=None)

    # ---- attendance, with regulars marked in place ----
    # One list, not two: a regular IS an attendee, and splitting them meant
    # reading the same person's name twice to answer one question. Minutes are
    # #co-working only, since that is what the threshold measures — a total
    # across every room couldn't be compared against it.
    cw = {t["user_id"]: t["seconds"] for t in
          voice_time_totals([FAIRACCESS_REGULAR_ROOM], _now(), limit=10_000)}
    regulars = {c["user_id"] for c in actives}
    # Regulars with no logged time still belong here — several were designated
    # by hand — so the roster is the union, not just whoever has minutes.
    listed = [t["user_id"] for t in totals]
    listed += [u for u in regulars if u not in listed]
    # Sorted by minutes alone, not regulars-first: the useful signal is who is
    # approaching the line, and someone at 480 shouldn't sit below a regular
    # designated by hand at zero.
    listed.sort(key=lambda u: cw.get(u, 0), reverse=True)

    feed_lines = []
    for uid in listed:
        secs = cw.get(uid, 0)
        line = f"<@{uid}>"
        if secs >= 60:
            line += f" · {secs // 60} min"
        if uid in regulars:
            line += " · **regular**"
        feed_lines.append(line)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("### Attendance"),
        discord.ui.TextDisplay("\n".join(feed_lines) or "*no time logged yet*"),
        discord.ui.TextDisplay(
            f"-# time in #{_room_name(bot, FAIRACCESS_REGULAR_ROOM)} · "
            f"regular at {FAIRACCESS_REGULAR_MINUTES} min · "
            "`/regular add` · `/regular remove`"),
        accent_color=0x4E5058))

    # ---- the host's own time across their rooms ----
    if STREAMER_DISCORD_ID:
        rooms = voice_time_by_channel(STREAMER_DISCORD_ID, _now(), VOICE_TIME_HOST_ROOMS)
        total = sum(r["seconds"] for r in rooms) // 60
        body = [f"**{total // 60}h {total % 60}m** total"]
        body += [f"#{_room_name(bot, r['channel_id'])} · {r['seconds'] // 60} min"
                 for r in rooms if r["seconds"] >= 60]

        # Week over week, newest first. Empty weeks are listed rather than
        # skipped — a gap is the point of the comparison.
        weekly = _weekly_totals(STREAMER_DISCORD_ID, VOICE_TIME_HOST_ROOMS, _now())
        wk_lines = []
        for i, (start, seconds) in enumerate(weekly):
            label = ("**this week**" if i == 0 else "last week" if i == 1
                     else f"wk of {datetime.fromtimestamp(start):%b %-d}")
            wk_lines.append(f"{label} · {_hm(seconds)}")

        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"### <@{STREAMER_DISCORD_ID}>'s voice time"),
            discord.ui.TextDisplay("\n".join(body) if rooms else "*no time logged yet*"),
            discord.ui.TextDisplay("\n".join(wk_lines)),
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

    # New regulars, before reconciling — a cooldown applied here should get its
    # overwrite checked in the same pass rather than waiting for the next one.
    changed |= await _apply_regulars(bot)

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
