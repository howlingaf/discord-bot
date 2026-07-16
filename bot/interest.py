"""Track who reacted to a given post with a given emoji.

The tracked post is made by someone else (e.g. Sapphire) — we only watch it.
Reactions on it stay public; the roster is just a private, always-current list
in a mods-only channel, rendered as a single message we keep editing.
"""

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


def emoji_key(emoji: "str | discord.PartialEmoji | discord.Emoji") -> str:
    """Stable identity for a unicode or custom emoji.

    Unicode emoji have no id, so the name (the character itself) is the key.
    Custom emoji reuse names across servers, so they key on name:id.
    """
    if isinstance(emoji, str):
        emoji = discord.PartialEmoji.from_str(emoji.strip())
    if emoji.id:
        return f"{emoji.name}:{emoji.id}"
    return emoji.name or ""


def emoji_display(key: str) -> str:
    """Render a stored emoji key back into something Discord will draw."""
    if ":" in key:
        name, _, eid = key.rpartition(":")
        return f"<:{name}:{eid}>"
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
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                return await channel.fetch_message(message_id), ""
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                continue
    return None, "Couldn't find that message id in any channel I can read."


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
    if live is None:
        live = []

    reaction_track_save(message.id, message.channel.id, key, roster_channel_id, None)
    added, _ = reaction_reactors_sync(message.id, live)

    roster_msg = await roster_channel.send(embed=await _build_embed(bot, message.id))
    reaction_track_set_roster_message(message.id, roster_msg.id)

    return True, (
        f"Tracking {emoji_display(key)} on that post — {added} existing reaction(s) picked up. "
        f"Roster: {roster_msg.jump_url}"
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


async def refresh_roster(bot, message_id: int) -> None:
    """Re-render the roster message in place."""
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


async def on_reaction_add(bot, payload: discord.RawReactionActionEvent) -> None:
    track = reaction_track_get(payload.message_id)
    if not track or emoji_key(payload.emoji) != track["emoji"]:
        return
    if payload.user_id == (bot.user.id if bot.user else 0):
        return
    if payload.member and payload.member.bot:
        return
    if reaction_reactor_add(payload.message_id, payload.user_id):
        await refresh_roster(bot, payload.message_id)


async def on_reaction_remove(bot, payload: discord.RawReactionActionEvent) -> None:
    track = reaction_track_get(payload.message_id)
    if not track or emoji_key(payload.emoji) != track["emoji"]:
        return
    if reaction_reactor_remove(payload.message_id, payload.user_id):
        await refresh_roster(bot, payload.message_id)


async def resync_all(bot) -> None:
    """Reconcile every tracked post against Discord.

    Reaction events that land while the bot is down are never replayed, so without
    this a restart would silently lose or keep stale reactors.
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

        live = await _live_reactors(message, track["emoji"])
        if live is None:
            live = []
        added, removed = reaction_reactors_sync(mid, live)
        if added or removed:
            print(f"[INTEREST] resync {mid}: +{added} -{removed}")
        await refresh_roster(bot, mid)
