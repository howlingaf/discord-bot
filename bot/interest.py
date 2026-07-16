"""Track who reacted to a given post with a given emoji.

The tracked post is made by someone else (e.g. Sapphire) — we only watch it.
Reactions on it stay public; the roster is just a private, always-current list
in a mods-only channel, rendered as a single message we keep editing.
"""

import asyncio
import re

import discord

from .config import GUILD_ID
from .database import (
    reaction_track_get,
    reaction_track_save,
    reaction_track_set_roster_message,
    reaction_tracks_all,
    reaction_reactor_add,
    reaction_reactor_remove,
    reaction_reactors_get,
    reaction_reactors_sync,
)
from .logbus import log_error

_LINK_RE = re.compile(r"channels/(?:\d+|@me)/(\d+)/(\d+)")

ROSTER_COLOR = 0x5865F2
# Embed descriptions cap at 4096 chars; stop well short and show a remainder note.
_MAX_LISTED = 80

# A roster render is fetch-then-edit, and reactions arrive in bursts. Serialize
# per tracked post so two renders can't both decide the roster is missing and
# post a duplicate, and so a resync's slow snapshot can't race the live
# reaction handlers writing the same rows.
_locks: dict[int, asyncio.Lock] = {}
# Coalesce a burst into one edit — the roster renders from the DB, so a single
# render after the burst reflects every write in it. Without this, each reaction
# is its own edit and they queue behind Discord's per-channel edit rate limit.
_dirty: set[int] = set()
_render_tasks: dict[int, asyncio.Task] = {}
_DEBOUNCE_SECONDS = 1.5


def _lock(message_id: int) -> asyncio.Lock:
    return _locks.setdefault(message_id, asyncio.Lock())


def emoji_key(emoji: "str | discord.PartialEmoji | discord.Emoji") -> str:
    """Stable identity for a unicode or custom emoji.

    Custom emoji key on id alone — the id is already globally unique, and a name
    is mutable, so including it would break matching the moment someone renames
    the emoji. Unicode emoji have no id, so the character itself is the key.
    A custom key is therefore all digits; a unicode one never is.
    """
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji.strip())
    if emoji.id:
        return str(emoji.id)
    return emoji.name or ""


def emoji_display(key: str) -> str:
    """Render a stored emoji key back into something Discord will draw.

    Discord resolves custom emoji by id and ignores the name in the markdown,
    so a placeholder name renders correctly.
    """
    if key.isdigit():
        return f"<:e:{key}>"
    return key


async def resolve_message(bot, ref: str) -> tuple[discord.Message | None, str]:
    """Resolve a message link or bare id to a Message.

    A bare id has no channel attached, so fall back to scanning the guild's
    text channels for it.
    """
    ref = (ref or "").strip()

    m = _LINK_RE.search(ref)
    if m:
        channel_id, message_id = int(m.group(1)), int(m.group(2))
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            return await channel.fetch_message(message_id), ""
        except discord.NotFound:
            return None, "That message link points at something I can't find."
        except discord.Forbidden:
            return None, "I don't have permission to read that channel."
        except Exception as e:
            return None, f"Couldn't fetch that message: {e}"

    if not ref.isdigit():
        return None, "Give me a message link or a message id."

    message_id = int(ref)
    for channel in await _text_channels(bot):
        try:
            return await channel.fetch_message(message_id), ""
        except (discord.NotFound, discord.Forbidden):
            continue
        except Exception:
            continue
    return None, "Couldn't find that message id in any channel I can read."


async def _text_channels(bot) -> list[discord.TextChannel]:
    """Text channels to scan for a bare message id.

    bot.guilds is only populated from the gateway cache, so fall back to fetching
    over HTTP — that's the path a one-off script without a gateway session takes.
    """
    if bot.guilds:
        return [ch for guild in bot.guilds for ch in guild.text_channels]
    try:
        guild = await bot.fetch_guild(GUILD_ID)
        return [ch for ch in await guild.fetch_channels() if isinstance(ch, discord.TextChannel)]
    except Exception:
        return []


async def _live_reactors(message: discord.Message, key: str) -> list[int] | None:
    """User ids currently reacting with `key`, or None if the emoji isn't on the post."""
    for reaction in message.reactions:
        if emoji_key(reaction.emoji) != key:
            continue
        return [user.id async for user in reaction.users() if not user.bot]
    return None


async def start_tracking(bot, message: discord.Message, emoji: str, roster_channel_id: int) -> tuple[bool, str]:
    """Begin tracking `emoji` on `message`, seeding from reactions already there."""
    key = emoji_key(emoji)
    if not key:
        return False, "That doesn't look like an emoji I can track."

    try:
        roster_channel = bot.get_channel(roster_channel_id) or await bot.fetch_channel(roster_channel_id)
    except Exception as e:
        return False, f"Couldn't reach the roster channel: {e}"

    live = await _live_reactors(message, key)

    # `live is None` means that emoji isn't on the post at all. For a post nobody
    # has reacted to yet that's fine, but if we already hold reactors for it, the
    # emoji argument is almost certainly wrong (a typo, or the shell mangling it)
    # — and syncing to an empty list would silently delete the whole roster.
    if live is None and reaction_reactors_get(message.id):
        return False, (
            f"{emoji_display(key)} isn't on that post, but I already have "
            f"{len(reaction_reactors_get(message.id))} reactor(s) recorded for it. "
            f"Refusing, since that would wipe the roster — check the emoji argument."
        )

    # Re-pointing at a post we already track should reuse its roster rather than
    # leave an orphaned message behind that silently stops updating.
    prior = reaction_track_get(message.id)
    existing_roster_id = None
    if prior and prior["roster_message_id"] and prior["roster_channel_id"] == roster_channel_id:
        try:
            await roster_channel.fetch_message(prior["roster_message_id"])
            existing_roster_id = prior["roster_message_id"]
        except Exception:
            existing_roster_id = None

    reaction_track_save(message.id, message.channel.id, key, roster_channel_id, existing_roster_id)
    added, removed = reaction_reactors_sync(message.id, live or [])

    if existing_roster_id:
        await refresh_roster(bot, message.id)
        roster_url = f"https://discord.com/channels/{GUILD_ID}/{roster_channel_id}/{existing_roster_id}"
    else:
        roster_msg = await roster_channel.send(embed=await _build_embed(bot, message.id))
        reaction_track_set_roster_message(message.id, roster_msg.id)
        roster_url = roster_msg.jump_url

    total = len(reaction_reactors_get(message.id))
    return True, (
        f"Tracking {emoji_display(key)} on that post — {total} reactor(s) "
        f"(+{added}/-{removed} this sync). Roster: {roster_url}"
    )


async def _build_embed(bot, message_id: int) -> discord.Embed:
    track = reaction_track_get(message_id)
    reactors = reaction_reactors_get(message_id)
    key = track["emoji"] if track else ""

    post_url = ""
    if track:
        post_url = f"https://discord.com/channels/{GUILD_ID}/{track['channel_id']}/{message_id}"

    lines = []
    for i, r in enumerate(reactors[:_MAX_LISTED], start=1):
        lines.append(f"`{i:>2}.` <@{r['user_id']}> · <t:{r['reacted_at']}:f>")
    if len(reactors) > _MAX_LISTED:
        lines.append(f"\n*…and {len(reactors) - _MAX_LISTED} more.*")

    body = "\n".join(lines) if lines else "*Nobody yet.*"
    if post_url:
        body = f"[Jump to the post]({post_url})\n\n{body}"

    embed = discord.Embed(
        title=f"{emoji_display(key)} Interested — {len(reactors)}",
        description=body,
        color=ROSTER_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Times are when the bot first saw the reaction · updates live")
    return embed


async def _render_roster(bot, message_id: int) -> None:
    """Re-render the roster message in place. Caller must hold the post's lock."""
    track = reaction_track_get(message_id)
    if not track or not track["roster_message_id"]:
        return
    try:
        channel = bot.get_channel(track["roster_channel_id"]) or await bot.fetch_channel(track["roster_channel_id"])
        roster_msg = await channel.fetch_message(track["roster_message_id"])
        await roster_msg.edit(embed=await _build_embed(bot, message_id))
    except discord.NotFound:
        # Roster message was deleted — post a fresh one rather than going silent.
        try:
            channel = bot.get_channel(track["roster_channel_id"]) or await bot.fetch_channel(track["roster_channel_id"])
            new_msg = await channel.send(embed=await _build_embed(bot, message_id))
            reaction_track_set_roster_message(message_id, new_msg.id)
        except Exception as e:
            log_error(f"[INTEREST] could not repost roster for {message_id}: {e}")
    except Exception as e:
        log_error(f"[INTEREST] roster refresh failed for {message_id}: {e}")


async def refresh_roster(bot, message_id: int) -> None:
    async with _lock(message_id):
        await _render_roster(bot, message_id)


async def _render_worker(bot, message_id: int) -> None:
    try:
        while True:
            # Checked and cleared without awaiting, so a schedule_refresh() can't
            # slip between this check and the task being dropped in `finally`.
            if message_id not in _dirty:
                return
            _dirty.discard(message_id)
            await asyncio.sleep(_DEBOUNCE_SECONDS)
            async with _lock(message_id):
                await _render_roster(bot, message_id)
    finally:
        _render_tasks.pop(message_id, None)


def schedule_refresh(bot, message_id: int) -> None:
    """Ask for a roster render, coalescing bursts into a single edit."""
    _dirty.add(message_id)
    task = _render_tasks.get(message_id)
    if task is None or task.done():
        _render_tasks[message_id] = asyncio.create_task(_render_worker(bot, message_id))


async def on_reaction_add(bot, payload: discord.RawReactionActionEvent) -> None:
    track = reaction_track_get(payload.message_id)
    if not track or emoji_key(payload.emoji) != track["emoji"]:
        return
    if payload.user_id == (bot.user.id if bot.user else 0):
        return
    if payload.member and payload.member.bot:
        return
    async with _lock(payload.message_id):
        changed = reaction_reactor_add(payload.message_id, payload.user_id)
    if changed:
        schedule_refresh(bot, payload.message_id)


async def on_reaction_remove(bot, payload: discord.RawReactionActionEvent) -> None:
    track = reaction_track_get(payload.message_id)
    if not track or emoji_key(payload.emoji) != track["emoji"]:
        return
    async with _lock(payload.message_id):
        changed = reaction_reactor_remove(payload.message_id, payload.user_id)
    if changed:
        schedule_refresh(bot, payload.message_id)


async def on_reaction_clear(bot, payload: discord.RawReactionClearEvent) -> None:
    """All reactions removed at once.

    Discord sends one bulk event instead of a remove per user, so without this the
    roster would keep listing everyone after a mod clears the post.
    """
    if not reaction_track_get(payload.message_id):
        return
    async with _lock(payload.message_id):
        _, removed = reaction_reactors_sync(payload.message_id, [])
    if removed:
        print(f"[INTEREST] reactions cleared on {payload.message_id}: -{removed}")
        schedule_refresh(bot, payload.message_id)


async def on_reaction_clear_emoji(bot, payload: discord.RawReactionClearEmojiEvent) -> None:
    """One emoji cleared from the post — same bulk-event problem as above."""
    track = reaction_track_get(payload.message_id)
    if not track or emoji_key(payload.emoji) != track["emoji"]:
        return
    async with _lock(payload.message_id):
        _, removed = reaction_reactors_sync(payload.message_id, [])
    if removed:
        print(f"[INTEREST] {track['emoji']} cleared on {payload.message_id}: -{removed}")
        schedule_refresh(bot, payload.message_id)


async def resync_all(bot) -> None:
    """Reconcile every tracked post against Discord.

    Reaction events that land while the bot is down are never replayed, so without
    this a restart would silently lose or keep stale reactors. Safe to run on every
    on_ready — a reconnect is exactly when events were missed.
    """
    for track in reaction_tracks_all():
        mid = track["message_id"]
        try:
            channel = bot.get_channel(track["channel_id"]) or await bot.fetch_channel(track["channel_id"])
            message = await channel.fetch_message(mid)
        except discord.NotFound:
            print(f"[INTEREST] tracked post {mid} is gone — leaving the roster as-is")
            continue
        except Exception as e:
            log_error(f"[INTEREST] resync could not fetch {mid}: {e}")
            continue

        # Snapshot and reconcile under the lock: fetching reactors takes several
        # round trips, and a reaction landing mid-snapshot would otherwise look
        # like a stored reactor who isn't live, and get deleted.
        try:
            async with _lock(mid):
                live = await _live_reactors(message, track["emoji"])
                added, removed = reaction_reactors_sync(mid, live or [])
                if added or removed:
                    print(f"[INTEREST] resync {mid}: +{added} -{removed}")
                await _render_roster(bot, mid)
        except Exception as e:
            log_error(f"[INTEREST] resync failed for {mid}: {e}")
