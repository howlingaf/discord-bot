"""One-off: backfill a silent Discord tag into existing "submitted a solution!"
comments for a given handle. Edits are sent with allowed_mentions=none, so the
added <@id> renders as a name-tag with NO ping and NO thread subscription.

Dry-run by default. Set APPLY=1 to actually edit.
"""
import asyncio
import os
import re

import discord

from bot.config import (
    TOKEN,
    GUILD_ID,
    SPOTIFY_ALLOWED_USER_ID,
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_WEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID,
)

HANDLE = "howlingaf"                 # the bolded name in the comments
DISCORD_ID = SPOTIFY_ALLOWED_USER_ID  # who to tag (the owner)

FORUM_CHANNEL_IDS = [
    LEETCODE_PROBLEMS_CHANNEL_ID,
    LEETCODE_WEEKLY_FORUM_CHANNEL_ID,
    LEETCODE_BIWEEKLY_FORUM_CHANNEL_ID,
]

APPLY = os.getenv("APPLY") == "1"

# Replace **handle** (optionally already tagged with " (<@id>)") with just the
# silent mention. Re-runs are safe: once converted there's no **handle** left.
_PAT = re.compile(r"\*\*" + re.escape(HANDLE) + r"\*\*(?: \(<@\d+>\))?")
SILENT = discord.AllowedMentions.none()


def retag(content: str) -> str:
    return _PAT.sub(f"<@{DISCORD_ID}>", content)


async def with_retry(factory, attempts=4):
    last = None
    for i in range(attempts):
        try:
            return await factory()
        except Exception as e:
            last = e
            await asyncio.sleep(1.5 * (i + 1))
    raise last


async def iter_threads(forum):
    seen = set()
    for t in forum.threads:
        if t.id not in seen:
            seen.add(t.id)
            yield t
    async for t in forum.archived_threads(limit=None):
        if t.id not in seen:
            seen.add(t.id)
            yield t


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print(f"Mode: {'APPLY (will edit)' if APPLY else 'DRY-RUN (no edits)'}")

    # Confirm the target id resolves to the expected person
    guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
    member = guild.get_member(DISCORD_ID) if guild else None
    if member is None:
        try:
            member = await guild.fetch_member(DISCORD_ID)
        except Exception:
            member = None
    print(f"Tagging <@{DISCORD_ID}> -> {member.display_name if member else '??? (could not resolve)'}")
    print(f"Handle in comments: **{HANDLE}**\n")

    matches = edited = threads = 0
    samples = []
    for cid in FORUM_CHANNEL_IDS:
        forum = client.get_channel(cid) or await client.fetch_channel(cid)
        print(f"== Forum {forum.name!r}")
        async for thread in iter_threads(forum):
            threads += 1
            try:
                msgs = await with_retry(lambda t=thread: collect(t))
            except Exception as e:
                print(f"  !! history failed {thread.id}: {e}")
                continue
            edits = []
            for m in msgs:
                if m.author.id != client.user.id:
                    continue
                new = retag(m.content)
                if new != m.content:
                    matches += 1
                    if len(samples) < 6:
                        samples.append(f"[{thread.name}]\n  OLD: {m.content!r}\n  NEW: {new!r}")
                    edits.append((m, new))
            if not edits or not APPLY:
                continue
            was_archived = thread.archived
            try:
                if was_archived:
                    await with_retry(lambda t=thread: t.edit(archived=False))
                for m, new in edits:
                    try:
                        await with_retry(lambda m=m, c=new: m.edit(content=c, allowed_mentions=SILENT))
                        edited += 1
                    except Exception as e:
                        print(f"  !! edit failed {m.id}: {e}")
            finally:
                if was_archived:
                    try:
                        await with_retry(lambda t=thread: t.edit(archived=True))
                    except Exception as e:
                        print(f"  !! re-archive failed {thread.id}: {e}")

    print(f"\nThreads scanned: {threads}")
    print(f"Matching messages: {matches}")
    if APPLY:
        print(f"Messages edited: {edited}")
    print("\n--- samples ---")
    for s in samples:
        print(s)
    if not APPLY and matches:
        print("\nDry-run only. Re-run with APPLY=1 to edit.")
    await client.close()


async def collect(thread):
    return [m async for m in thread.history(limit=None)]


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN")
    if not DISCORD_ID:
        raise SystemExit("SPOTIFY_ALLOWED_USER_ID (owner) not set")
    client.run(TOKEN)
