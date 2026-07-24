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
    state on every change and at startup. Buttons are DynamicItems (restart-safe
    with no stored ids), mirrored by /whitelist and /cooldown slash commands.
"""

import asyncio
import json
import time

import discord

from .config import (
    FAIRACCESS_ADMIN_CHANNEL_ID,
    FAIRACCESS_COOLDOWN_DAYS,
    FAIRACCESS_MOD_ROLE_ID,
    FAIRACCESS_THRESHOLD_MINUTES,
    FAIRACCESS_TRACKED_ROOMS,
    FAIRACCESS_VERIFIED_ROLE_ID,
    FAIRACCESS_WINDOW_RESET_HOURS,
    GUILD_ID,
)
from .database import (
    fairaccess_cooldown_active_for,
    fairaccess_cooldown_create,
    fairaccess_cooldown_mark_expired,
    fairaccess_cooldown_release,
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
    fairaccess_windows_recent,
)
from .logbus import log_error, log_if_persistent

_SWEEP_SECONDS = 300
_FEED_LIMIT = 15
# component budget on the panel message: 5 rows x 5 buttons
# inline Section rows cost 3 components each (40-component message cap)
_MAX_WL_INLINE = 4
_MAX_RELEASE_INLINE = 5

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


async def _log(bot, text: str) -> None:
    """Append-only action log under the panel. Best-effort, never raises."""
    try:
        ch = await _admin_channel(bot)
        await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        log_error(f"[FAIRACCESS] admin log failed: {e!r}")


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
    for cid in FAIRACCESS_TRACKED_ROOMS:
        try:
            await _apply_overwrite(bot, cid, user_id)
        except Exception as e:
            log_error(f"[FAIRACCESS] apply overwrite failed ({cid}/{user_id}): {e!r}")


async def _remove_all(bot, user_id: int) -> None:
    for cid in FAIRACCESS_TRACKED_ROOMS:
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
        if member.bot or not FAIRACCESS_TRACKED_ROOMS:
            return
        b = before.channel.id if before.channel else None
        a = after.channel.id if after.channel else None
        if b == a:
            return  # mute/deafen/stream toggles fire this event too
        tracked = set(FAIRACCESS_TRACKED_ROOMS)
        left = b if b in tracked else None
        joined = a if a in tracked else None
        if left is None and joined is None:
            return
        async with _lock:
            changed = await _handle_voice(bot, member, left, joined)
        if changed:
            await render_panel(bot)
    except Exception as e:
        log_error(f"[FAIRACCESS] voice handler failed: {e!r}")


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
    by = f" by <@{applied_by}>" if applied_by else ""
    await _log(bot, f"🚫 Cooldown applied{by}: <@{user_id}> — "
                    f"{_fmt_minutes(bot, rooms)} · expires <t:{expires}:R>")


# ------------------------------------------------------------------ #
#  Staff actions (shared by buttons and slash commands)              #
# ------------------------------------------------------------------ #

async def whitelist_add(bot, user_id: int, added_by: int) -> tuple[bool, str]:
    async with _lock:
        if not fairaccess_whitelist_add(user_id, added_by):
            return False, "Already whitelisted."
    await _log(bot, f"➕ Whitelisted: <@{user_id}> (by <@{added_by}>)")
    await render_panel(bot)
    return True, f"Whitelisted <@{user_id}>."


async def whitelist_remove(bot, user_id: int, removed_by: int) -> tuple[bool, str]:
    async with _lock:
        if not fairaccess_whitelist_remove(user_id):
            return False, "Not on the whitelist."
        # zero their tally: minutes accrued while exempt must not flag them
        w = fairaccess_window_open_for(user_id)
        if w:
            _close_window(w, _now(), status="exempt")
    await _log(bot, f"➖ Removed from whitelist: <@{user_id}> (by <@{removed_by}>, tally reset)")
    await render_panel(bot)
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
    await _log(bot, f"📥 Whitelist seeded from @{role.name}: {added} added, "
                    f"{len(role.members) - added} already present (by <@{seeded_by}>)")
    await render_panel(bot)
    return True, f"Seeded {added} member(s) from @{role.name}."


async def session_reset(bot, user_id: int, reset_by: int) -> tuple[bool, str]:
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
    await _log(bot, f"🔄 Session tally reset: <@{user_id}> — was "
                    f"{_fmt_minutes(bot, rooms)} (by <@{reset_by}>)")
    await render_panel(bot)
    return True, f"Reset <@{user_id}>'s tally (was {_fmt_minutes(bot, rooms)})."


async def cooldown_release(bot, user_id: int, released_by: int) -> tuple[bool, str]:
    async with _lock:
        cd = fairaccess_cooldown_active_for(user_id)
        if not cd:
            return False, "No active cooldown for that user."
        fairaccess_cooldown_release(cd["id"], released_by, _now())
    await _remove_all(bot, user_id)
    await _log(bot, f"🔓 Early release: <@{user_id}> (by <@{released_by}>)")
    await render_panel(bot)
    return True, f"Released <@{user_id}>."


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

def _staff_check(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_messages))


class WhitelistRemoveButton(discord.ui.DynamicItem[discord.ui.Button],
                            template=r"fa:wlrm:(?P<uid>\d+)"):
    def __init__(self, uid: int, name: str = ""):
        super().__init__(discord.ui.Button(
            label=f"WL − {name}"[:80] if name else "WL remove",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fa:wlrm:{uid}",
        ))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not _staff_check(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return
        _, msg = await whitelist_remove(interaction.client, self.uid, interaction.user.id)
        await interaction.response.send_message(msg, ephemeral=True)


class ReleaseButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"fa:rel:(?P<uid>\d+)"):
    def __init__(self, uid: int, name: str = ""):
        super().__init__(discord.ui.Button(
            label=f"Release {name}"[:80] if name else "Release",
            style=discord.ButtonStyle.danger,
            custom_id=f"fa:rel:{uid}",
        ))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not _staff_check(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return
        _, msg = await cooldown_release(interaction.client, self.uid, interaction.user.id)
        await interaction.response.send_message(msg, ephemeral=True)


class SessionResetButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"fa:rst:(?P<uid>\d+)"):
    def __init__(self, uid: int, name: str = ""):
        super().__init__(discord.ui.Button(
            label=f"Reset {name}"[:80] if name else "Reset tally",
            style=discord.ButtonStyle.primary,
            custom_id=f"fa:rst:{uid}",
        ))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if not _staff_check(interaction):
            await interaction.response.send_message("❌ Staff only.", ephemeral=True)
            return
        _, msg = await session_reset(interaction.client, self.uid, interaction.user.id)
        await interaction.response.send_message(msg, ephemeral=True)


_render_lock = asyncio.Lock()


def _build_panel(bot) -> discord.ui.LayoutView:
    """Components-V2 layout: each actionable row carries its button inline
    (Section accessory), so the panel spends no vertical space on button rows.
    Buttons are plain items whose custom_ids match the registered DynamicItem
    templates — routing works regardless of which render created the message.
    Budget: Discord caps a message at 40 components; a Section row costs 3."""
    wl = fairaccess_whitelist_all()
    actives = fairaccess_cooldowns_active()
    recent = fairaccess_windows_recent(_FEED_LIMIT)

    view = discord.ui.LayoutView(timeout=None)

    # ---- whitelist (inline Remove for the first few; rest text + command) ----
    wl_items = [discord.ui.TextDisplay("### Fair access — whitelist")]
    for r in wl[:_MAX_WL_INLINE]:
        wl_items.append(discord.ui.Section(
            f"<@{r['user_id']}> · added <t:{r['added_at']}:d> by <@{r['added_by']}>",
            accessory=discord.ui.Button(label="Remove", style=discord.ButtonStyle.secondary,
                                        custom_id=f"fa:wlrm:{r['user_id']}")))
    if len(wl) > _MAX_WL_INLINE:
        extra = "\n".join(f"<@{r['user_id']}> · added <t:{r['added_at']}:d>"
                          for r in wl[_MAX_WL_INLINE:_MAX_WL_INLINE + 20])
        more = len(wl) - _MAX_WL_INLINE - 20
        wl_items.append(discord.ui.TextDisplay(
            extra + (f"\n-# …and {more} more" if more > 0 else "")
            + "\n-# remove via `/whitelist remove`"))
    if not wl:
        wl_items.append(discord.ui.TextDisplay("*empty*"))
    view.add_item(discord.ui.Container(*wl_items, accent_color=0x43B581))

    # ---- active cooldowns (name + release time + inline Release) ----
    cd_items = [discord.ui.TextDisplay("### Active cooldowns")]
    for c in actives[:_MAX_RELEASE_INLINE]:
        cd_items.append(discord.ui.Section(
            f"<@{c['user_id']}> · releases <t:{c['expires_at']}:R>",
            accessory=discord.ui.Button(label="Release", style=discord.ButtonStyle.danger,
                                        custom_id=f"fa:rel:{c['user_id']}")))
    if len(actives) > _MAX_RELEASE_INLINE:
        cd_items.append(discord.ui.TextDisplay("\n".join(
            f"<@{c['user_id']}> · releases <t:{c['expires_at']}:R>"
            for c in actives[_MAX_RELEASE_INLINE:]) + "\n-# release via `/cooldown release`"))
    if not actives:
        cd_items.append(discord.ui.TextDisplay("*none*"))
    view.add_item(discord.ui.Container(*cd_items, accent_color=0xED4245))

    # ---- visitor feed (text only; tallies reset via /cooldown reset) ----
    feed_lines = []
    for w in recent:
        rooms = json.loads(w["room_seconds"])
        if w["status"] == "open" and w["last_join_at"]:
            state = "in room"
        else:
            state = {"open": "tallying", "ok": "ok", "exempt": "exempt",
                     "flagged": "🚫 flagged"}.get(w["status"], w["status"])
        feed_lines.append(
            f"<@{w['user_id']}> · {_fmt_minutes(bot, rooms)} · {state} · <t:{w['last_activity_at']}:R>")
    rooms_line = ", ".join(f"#{_room_name(bot, cid)}" for cid in FAIRACCESS_TRACKED_ROOMS)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"### Recent visitors (last {_FEED_LIMIT} sessions)"),
        discord.ui.TextDisplay("\n".join(feed_lines) or "*no sessions yet*"),
        discord.ui.TextDisplay(f"-# Tracked: {rooms_line} · threshold "
                               f"{FAIRACCESS_THRESHOLD_MINUTES} min · cooldown "
                               f"{FAIRACCESS_COOLDOWN_DAYS} d · reset tallies via `/cooldown reset`"),
        accent_color=0x4E5058))

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
    changed = False

    for cd in fairaccess_cooldowns_due():
        fairaccess_cooldown_mark_expired(cd["id"], now)
        await _remove_all(bot, cd["user_id"])
        await _log(bot, f"✅ Cooldown expired: <@{cd['user_id']}>")
        changed = True

    changed |= _reset_stale_windows(now)

    # reconcile: DB is truth. Re-apply missing overwrites; drop orphans that
    # exactly match our signature.
    active_ids = {c["user_id"] for c in fairaccess_cooldowns_active()}
    for cid in FAIRACCESS_TRACKED_ROOMS:
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
    bot.add_dynamic_items(WhitelistRemoveButton, ReleaseButton, SessionResetButton)
    bot.loop.create_task(_loop(bot))
