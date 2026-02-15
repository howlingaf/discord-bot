import secrets
import sqlite3
import time

from .config import DB_PATH


def _db():
    return sqlite3.connect(DB_PATH)


def db_init():
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS verify_state (
          state TEXT PRIMARY KEY,
          discord_user_id INTEGER NOT NULL,
          expires_at INTEGER NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_tokens (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          access_token TEXT,
          refresh_token TEXT,
          expires_at INTEGER,
          updated_at INTEGER NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_runtime (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          paused_by_bot INTEGER NOT NULL DEFAULT 0,
          last_action_at INTEGER NOT NULL DEFAULT 0,
          last_member_count INTEGER NOT NULL DEFAULT -1
        )
        """)
        conn.execute("INSERT OR IGNORE INTO spotify_runtime(id, paused_by_bot, last_action_at, last_member_count) VALUES(1,0,0,-1)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_problems (
          question_id TEXT PRIMARY KEY,
          title_slug TEXT NOT NULL,
          title TEXT NOT NULL,
          thread_id INTEGER NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_daily_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          question_id TEXT,
          title_slug TEXT,
          title TEXT,
          date INTEGER
        )
        """)
        conn.execute("INSERT OR IGNORE INTO leetcode_daily_state(id) VALUES(1)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_contest_state (
          contest_type TEXT PRIMARY KEY,
          last_title_slug TEXT,
          updated_at INTEGER NOT NULL
        )
        """)

        conn.commit()


# ---- OAuth state helpers (used by Spotify) ----

def create_state(discord_user_id: int, ttl_sec: int = 15 * 60) -> str:
    state = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + ttl_sec
    with _db() as conn:
        conn.execute(
            "INSERT INTO verify_state(state, discord_user_id, expires_at) VALUES(?,?,?)",
            (state, discord_user_id, expires_at),
        )
        conn.commit()
    return state


def consume_state(state: str) -> int | None:
    now = int(time.time())
    with _db() as conn:
        row = conn.execute(
            "SELECT discord_user_id, expires_at FROM verify_state WHERE state=?",
            (state,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM verify_state WHERE state=?", (state,))
        conn.commit()

    discord_user_id, expires_at = int(row[0]), int(row[1])
    return discord_user_id if expires_at >= now else None


# ---- Spotify DB helpers ----

def spotify_get_tokens() -> tuple[str | None, str | None, int | None]:
    with _db() as conn:
        row = conn.execute("SELECT access_token, refresh_token, expires_at FROM spotify_tokens WHERE id=1").fetchone()
        if not row:
            return None, None, None
        return row[0], row[1], row[2]


def spotify_upsert_tokens(access_token: str, refresh_token: str | None, expires_in: int):
    now = int(time.time())
    expires_at = now + int(expires_in) - 15  # 15s safety buffer
    with _db() as conn:
        existing = conn.execute("SELECT refresh_token FROM spotify_tokens WHERE id=1").fetchone()
        existing_rt = existing[0] if existing else None
        rt = refresh_token or existing_rt

        conn.execute("""
        INSERT INTO spotify_tokens(id, access_token, refresh_token, expires_at, updated_at)
        VALUES(1,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          access_token=excluded.access_token,
          refresh_token=excluded.refresh_token,
          expires_at=excluded.expires_at,
          updated_at=excluded.updated_at
        """, (access_token, rt, expires_at, now))
        conn.commit()


def spotify_get_runtime() -> tuple[bool, int, int]:
    with _db() as conn:
        row = conn.execute("SELECT paused_by_bot, last_action_at, last_member_count FROM spotify_runtime WHERE id=1").fetchone()
        paused_by_bot = bool(row[0])
        return paused_by_bot, int(row[1]), int(row[2])


def spotify_set_runtime(*, paused_by_bot: bool | None = None, last_action_at: int | None = None, last_member_count: int | None = None):
    with _db() as conn:
        if paused_by_bot is not None:
            conn.execute("UPDATE spotify_runtime SET paused_by_bot=? WHERE id=1", (1 if paused_by_bot else 0,))
        if last_action_at is not None:
            conn.execute("UPDATE spotify_runtime SET last_action_at=? WHERE id=1", (int(last_action_at),))
        if last_member_count is not None:
            conn.execute("UPDATE spotify_runtime SET last_member_count=? WHERE id=1", (int(last_member_count),))
        conn.commit()


# ---- LeetCode Problem helpers ----

def leetcode_get_problem(question_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT question_id, title_slug, title, thread_id FROM leetcode_problems WHERE question_id=?",
            (question_id,),
        ).fetchone()
        if not row:
            return None
        return {"question_id": row[0], "title_slug": row[1], "title": row[2], "thread_id": row[3]}


def leetcode_save_problem(*, question_id: str, title_slug: str, title: str, thread_id: int):
    with _db() as conn:
        conn.execute(
            """INSERT INTO leetcode_problems(question_id, title_slug, title, thread_id)
               VALUES(?,?,?,?)
               ON CONFLICT(question_id) DO UPDATE SET
                 thread_id=excluded.thread_id""",
            (question_id, title_slug, title, thread_id),
        )
        conn.commit()


# ---- LeetCode Daily state helpers ----

def leetcode_get_daily_state() -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT question_id, title_slug, title, date FROM leetcode_daily_state WHERE id=1").fetchone()
        if not row or not row[0]:
            return None
        return {"question_id": row[0], "title_slug": row[1], "title": row[2], "date": row[3]}


def leetcode_set_daily_state(*, question_id: str, title_slug: str, title: str, date: int):
    with _db() as conn:
        conn.execute(
            "UPDATE leetcode_daily_state SET question_id=?, title_slug=?, title=?, date=? WHERE id=1",
            (question_id, title_slug, title, date),
        )
        conn.commit()


# ---- LeetCode Contest DB helpers ----

def leetcode_get_contest_state(contest_type: str) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT last_title_slug FROM leetcode_contest_state WHERE contest_type=?",
            (contest_type,),
        ).fetchone()
        if not row:
            return None
        return row[0]


def leetcode_set_contest_state(contest_type: str, last_title_slug: str):
    now = int(time.time())
    with _db() as conn:
        conn.execute(
            """INSERT INTO leetcode_contest_state(contest_type, last_title_slug, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(contest_type) DO UPDATE SET
                 last_title_slug=excluded.last_title_slug,
                 updated_at=excluded.updated_at""",
            (contest_type, last_title_slug, now),
        )
        conn.commit()
