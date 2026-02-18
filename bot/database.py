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
          thread_id INTEGER NOT NULL,
          difficulty TEXT
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

        # Migrate: add columns that may be missing from older schema
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(leetcode_daily_state)")}
        for col in ("question_id TEXT", "title_slug TEXT", "title TEXT", "date INTEGER"):
            name = col.split()[0]
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE leetcode_daily_state ADD COLUMN {col}")
                print(f"[DB] Added missing column '{name}' to leetcode_daily_state")

        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(leetcode_problems)")}
        if "difficulty" not in existing_cols:
            conn.execute("ALTER TABLE leetcode_problems ADD COLUMN difficulty TEXT")
            print("[DB] Added missing column 'difficulty' to leetcode_problems")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_contest_state (
          contest_type TEXT PRIMARY KEY,
          last_title_slug TEXT,
          updated_at INTEGER NOT NULL
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS linked_users (
          discord_user_id INTEGER PRIMARY KEY,
          leetcode_username TEXT NOT NULL UNIQUE
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_status_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          message_id INTEGER,
          last_status TEXT
        )
        """)
        conn.execute("INSERT OR IGNORE INTO leetcode_status_state(id) VALUES(1)")

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
            "SELECT question_id, title_slug, title, thread_id, difficulty FROM leetcode_problems WHERE question_id=?",
            (question_id,),
        ).fetchone()
        if not row:
            return None
        return {"question_id": row[0], "title_slug": row[1], "title": row[2], "thread_id": row[3], "difficulty": row[4]}


def leetcode_get_problem_by_slug(title_slug: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT question_id, title_slug, title, thread_id, difficulty FROM leetcode_problems WHERE title_slug=?",
            (title_slug,),
        ).fetchone()
        if not row:
            return None
        return {"question_id": row[0], "title_slug": row[1], "title": row[2], "thread_id": row[3], "difficulty": row[4]}


def leetcode_save_problem(*, question_id: str, title_slug: str, title: str, thread_id: int, difficulty: str | None = None):
    with _db() as conn:
        conn.execute(
            """INSERT INTO leetcode_problems(question_id, title_slug, title, thread_id, difficulty)
               VALUES(?,?,?,?,?)
               ON CONFLICT(question_id) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 difficulty=COALESCE(excluded.difficulty, difficulty)""",
            (question_id, title_slug, title, thread_id, difficulty),
        )
        conn.commit()


def leetcode_delete_problem(question_id: str) -> dict | None:
    """Delete a problem by ID. Returns the deleted row or None if not found."""
    with _db() as conn:
        row = conn.execute(
            "SELECT question_id, title_slug, title, thread_id FROM leetcode_problems WHERE question_id=?",
            (question_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM leetcode_problems WHERE question_id=?", (question_id,))
        conn.commit()
        return {"question_id": row[0], "title_slug": row[1], "title": row[2], "thread_id": row[3]}


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


# ---- Linked users helpers ----

def linked_users_get(discord_user_id: int) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT leetcode_username FROM linked_users WHERE discord_user_id=?",
            (discord_user_id,),
        ).fetchone()
        return row[0] if row else None


def linked_users_get_by_username(leetcode_username: str) -> int | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT discord_user_id FROM linked_users WHERE leetcode_username=?",
            (leetcode_username,),
        ).fetchone()
        return row[0] if row else None


def linked_users_set(discord_user_id: int, leetcode_username: str):
    with _db() as conn:
        conn.execute(
            """INSERT INTO linked_users(discord_user_id, leetcode_username)
               VALUES(?,?)
               ON CONFLICT(discord_user_id) DO UPDATE SET
                 leetcode_username=excluded.leetcode_username""",
            (discord_user_id, leetcode_username),
        )
        conn.commit()


def linked_users_delete(discord_user_id: int) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM linked_users WHERE discord_user_id=?",
            (discord_user_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def linked_users_all() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT discord_user_id, leetcode_username FROM linked_users"
        ).fetchall()
        return [{"discord_user_id": r[0], "leetcode_username": r[1]} for r in rows]


# ---- LeetCode Status helpers ----

def leetcode_status_get() -> tuple[int | None, str | None]:
    with _db() as conn:
        row = conn.execute("SELECT message_id, last_status FROM leetcode_status_state WHERE id=1").fetchone()
        if not row:
            return None, None
        return row[0], row[1]


def leetcode_status_set(*, message_id: int | None = None, last_status: str | None = None):
    with _db() as conn:
        if message_id is not None:
            conn.execute("UPDATE leetcode_status_state SET message_id=? WHERE id=1", (message_id,))
        if last_status is not None:
            conn.execute("UPDATE leetcode_status_state SET last_status=? WHERE id=1", (last_status,))
        conn.commit()
