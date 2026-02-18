"""
One-time script to apply difficulty tags to all existing LeetCode problem threads.
Run from the project root: python backfill_difficulty_tags.py
"""
import asyncio
import os
import sqlite3

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "overlay.db")
LEETCODE_PROBLEMS_CHANNEL_ID = 1472231552607064144
LEETCODE_PROBLEM_URL = "https://leetcode-api-pied.vercel.app/problem/{qid}"


def get_all_problems():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT question_id, title, thread_id, difficulty FROM leetcode_problems").fetchall()
    conn.close()
    return [{"question_id": r[0], "title": r[1], "thread_id": r[2], "difficulty": r[3]} for r in rows]


def save_difficulty(question_id: str, difficulty: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE leetcode_problems SET difficulty=? WHERE question_id=?", (difficulty, question_id))
    conn.commit()
    conn.close()


async def fetch_difficulty(session: aiohttp.ClientSession, question_id: str) -> str | None:
    url = LEETCODE_PROBLEM_URL.format(qid=question_id)
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            js = await resp.json()
            return js.get("difficulty")
    except Exception as e:
        print(f"  API error for #{question_id}: {e}")
        return None


async def main():
    problems = get_all_problems()
    print(f"Found {len(problems)} problems in DB\n")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}\n")

        forum = client.get_channel(LEETCODE_PROBLEMS_CHANNEL_ID) or await client.fetch_channel(LEETCODE_PROBLEMS_CHANNEL_ID)
        if not isinstance(forum, discord.ForumChannel):
            print("ERROR: problems channel is not a forum channel")
            await client.close()
            return

        diff_tags = {t.name: t for t in forum.available_tags if t.name in ("Easy", "Medium", "Hard")}
        print(f"Difficulty tags found on channel: {list(diff_tags.keys())}\n")

        async with aiohttp.ClientSession() as session:
            for problem in problems:
                qid = problem["question_id"]
                title = problem["title"]
                thread_id = problem["thread_id"]

                print(f"#{qid} {title}")

                difficulty = problem["difficulty"]
                if not difficulty:
                    difficulty = await fetch_difficulty(session, qid)
                    if not difficulty:
                        print(f"  skipped (could not get difficulty)")
                        continue
                    save_difficulty(qid, difficulty)

                if difficulty not in diff_tags:
                    print(f"  skipped (unknown difficulty={difficulty})")
                    continue

                tag = diff_tags[difficulty]

                try:
                    thread = client.get_channel(thread_id) or await client.fetch_channel(thread_id)
                    if not isinstance(thread, discord.Thread):
                        print(f"  skipped (not a thread)")
                        continue

                    if any(t.id == tag.id for t in thread.applied_tags):
                        print(f"  already tagged ({difficulty})")
                        continue

                    new_tags = [t for t in thread.applied_tags if t.name not in ("Easy", "Medium", "Hard")] + [tag]
                    await thread.edit(applied_tags=new_tags)
                    print(f"  tagged as {difficulty}")
                except Exception as e:
                    print(f"  error: {e}")

                await asyncio.sleep(0.5)

        print("\nDone!")
        await client.close()

    await client.start(TOKEN)


asyncio.run(main())
