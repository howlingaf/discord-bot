import secrets
import sqlite3
import time

from .config import DB_PATH


def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # WAL lets a reader and the writer proceed without blocking each other, and
    # busy_timeout makes a contended connection wait briefly instead of raising
    # "database is locked" outright. check_same_thread=False so connections can
    # be used from a worker thread once DB calls are moved off the event loop.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


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
          updated_at INTEGER NOT NULL,
          thread_id INTEGER
        )
        """)

        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(leetcode_contest_state)")}
        if "thread_id" not in existing_cols:
            conn.execute("ALTER TABLE leetcode_contest_state ADD COLUMN thread_id INTEGER")
            print("[DB] Added missing column 'thread_id' to leetcode_contest_state")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS twitch_links (
          twitch_username TEXT PRIMARY KEY,       -- normalized: lowercase
          discord_user_id INTEGER,                -- NULL until linked
          status          TEXT NOT NULL,          -- 'pending' | 'linked' | 'dismissed'
          updated_at      INTEGER NOT NULL DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_premium_weekly_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          question_id TEXT,
          title_slug TEXT,
          date TEXT
        )
        """)
        conn.execute("INSERT OR IGNORE INTO leetcode_premium_weekly_state(id) VALUES(1)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS leetcode_contest_posts (
          contest_slug       TEXT PRIMARY KEY,
          contest_type       TEXT NOT NULL,
          thread_id          INTEGER NOT NULL,
          created_at         INTEGER NOT NULL DEFAULT 0,
          start_time         INTEGER NOT NULL DEFAULT 0,
          rated              INTEGER NOT NULL DEFAULT 0,
          problems_posted    INTEGER NOT NULL DEFAULT 0,
          problems_posted_at INTEGER
        )
        """)

        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(leetcode_contest_posts)")}
        for col in (
            "start_time INTEGER NOT NULL DEFAULT 0",
            "rated INTEGER NOT NULL DEFAULT 0",
            "problems_posted INTEGER NOT NULL DEFAULT 0",
            "problems_posted_at INTEGER",
        ):
            name = col.split()[0]
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE leetcode_contest_posts ADD COLUMN {col}")
                print(f"[DB] Added missing column '{name}' to leetcode_contest_posts")

        # ---- Zerotrac cache ----
        conn.execute("""
        CREATE TABLE IF NOT EXISTS zerotrac_cache (
          title_slug    TEXT PRIMARY KEY,
          rating        REAL NOT NULL,
          contest_slug  TEXT NOT NULL,
          problem_index TEXT NOT NULL,
          updated_at    INTEGER NOT NULL DEFAULT 0
        )
        """)

        # ---- Indexes for hot query paths ----
        # The contest poller filters on these columns every cycle; leetcode_problems
        # is looked up by slug on the recap path.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_posts_rated ON leetcode_contest_posts(rated)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_posts_type ON leetcode_contest_posts(contest_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_problems_slug ON leetcode_problems(title_slug)")

        # Drop verify_state rows that have already expired (they're unusable and
        # were previously only deleted on consume, leaking rows over time).
        conn.execute("DELETE FROM verify_state WHERE expires_at < ?", (int(time.time()),))

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
        # Atomic delete-and-return so two concurrent callbacks can't both consume
        # the same token (the first DELETE wins; the second sees no row).
        row = conn.execute(
            "DELETE FROM verify_state WHERE state=? RETURNING discord_user_id, expires_at",
            (state,),
        ).fetchone()
        conn.commit()

    if not row:
        return None
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


def leetcode_set_contest_state(contest_type: str, last_title_slug: str, *, thread_id: int | None = None):
    now = int(time.time())
    with _db() as conn:
        conn.execute(
            """INSERT INTO leetcode_contest_state(contest_type, last_title_slug, updated_at, thread_id)
               VALUES(?,?,?,?)
               ON CONFLICT(contest_type) DO UPDATE SET
                 last_title_slug=excluded.last_title_slug,
                 updated_at=excluded.updated_at,
                 thread_id=excluded.thread_id""",
            (contest_type, last_title_slug, now, thread_id),
        )
        conn.commit()


# ---- Twitch <-> Discord link helpers ----

def twitch_link_get(twitch_username: str) -> dict | None:
    """Hot path. Returns {'discord_user_id', 'status'} or None if never seen."""
    with _db() as conn:
        row = conn.execute(
            "SELECT discord_user_id, status FROM twitch_links WHERE twitch_username=?",
            (twitch_username,),
        ).fetchone()
        if not row:
            return None
        return {"discord_user_id": row[0], "status": row[1]}


def twitch_link_create_pending(twitch_username: str) -> bool:
    """Atomically claim a handle as 'pending'. Returns True only if WE created the
    row (caller should post the prompt); False if it already existed."""
    with _db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO twitch_links(twitch_username, discord_user_id, status, updated_at)
               VALUES(?, NULL, 'pending', ?)""",
            (twitch_username, int(time.time())),
        )
        conn.commit()
        return cur.rowcount > 0


def twitch_link_set_status(twitch_username: str, status: str, discord_user_id: int | None):
    """Used by the approve/dismiss callbacks. Idempotent upsert."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO twitch_links(twitch_username, discord_user_id, status, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(twitch_username) DO UPDATE SET
                 discord_user_id=excluded.discord_user_id,
                 status=excluded.status,
                 updated_at=excluded.updated_at""",
            (twitch_username, discord_user_id, status, int(time.time())),
        )
        conn.commit()


def twitch_link_delete(twitch_username: str) -> bool:
    """Forget a handle entirely so it can be re-prompted (used by /twitch-unlink)."""
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM twitch_links WHERE twitch_username=?",
            (twitch_username,),
        )
        conn.commit()
        return cur.rowcount > 0


# ---- LeetCode Premium Weekly helpers ----

def leetcode_get_premium_weekly_state() -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT question_id, title_slug, date FROM leetcode_premium_weekly_state WHERE id=1"
        ).fetchone()
        if not row or not row[0]:
            return None
        return {"question_id": row[0], "title_slug": row[1], "date": row[2]}


def leetcode_set_premium_weekly_state(*, question_id: str, title_slug: str, date: str):
    with _db() as conn:
        conn.execute(
            "UPDATE leetcode_premium_weekly_state SET question_id=?, title_slug=?, date=? WHERE id=1",
            (question_id, title_slug, date),
        )
        conn.commit()


# ---- LeetCode Contest Posts helpers ----

def leetcode_contest_post_get(contest_slug: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT contest_slug, contest_type, thread_id, created_at, start_time, problems_posted, problems_posted_at FROM leetcode_contest_posts WHERE contest_slug=?",
            (contest_slug,),
        ).fetchone()
        if not row:
            return None
        return {
            "contest_slug": row[0], "contest_type": row[1], "thread_id": row[2],
            "created_at": row[3], "start_time": row[4],
            "problems_posted": bool(row[5]), "problems_posted_at": row[6],
        }


def leetcode_contest_post_save(contest_slug: str, contest_type: str, thread_id: int, *, start_time: int = 0, rated: int = 0):
    now = int(time.time())
    with _db() as conn:
        conn.execute(
            """INSERT INTO leetcode_contest_posts(contest_slug, contest_type, thread_id, created_at, start_time, rated)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(contest_slug) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 contest_type=excluded.contest_type,
                 start_time=excluded.start_time,
                 rated=excluded.rated""",
            (contest_slug, contest_type, thread_id, now, start_time, rated),
        )
        conn.commit()


def leetcode_contest_post_set_problems_posted(contest_slug: str, timestamp: int):
    """Mark problems as posted. timestamp=0 means gave up (2h timeout), nonzero = success."""
    with _db() as conn:
        conn.execute(
            "UPDATE leetcode_contest_posts SET problems_posted=1, problems_posted_at=? WHERE contest_slug=?",
            (timestamp, contest_slug),
        )
        conn.commit()


def leetcode_contest_post_set_rated(contest_slug: str):
    with _db() as conn:
        conn.execute("UPDATE leetcode_contest_posts SET rated=1 WHERE contest_slug=?", (contest_slug,))
        conn.commit()


def leetcode_contest_posts_get_unrated() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT contest_slug, contest_type, thread_id, start_time FROM leetcode_contest_posts WHERE rated=0"
        ).fetchall()
        return [{"contest_slug": r[0], "contest_type": r[1], "thread_id": r[2], "start_time": r[3]} for r in rows]


def leetcode_contest_posts_delete_by_type(contest_type: str) -> int:
    """Delete all contest post records for a given type. Returns rows deleted."""
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM leetcode_contest_posts WHERE contest_type=?", (contest_type,)
        )
        conn.commit()
        return cur.rowcount


# ---- Zerotrac cache helpers ----

def zerotrac_cache_get_all() -> dict[str, dict]:
    """Return all cached zerotrac entries keyed by title_slug."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT title_slug, rating, contest_slug, problem_index FROM zerotrac_cache"
        ).fetchall()
        return {r[0]: {"title_slug": r[0], "rating": r[1], "contest_slug": r[2], "problem_index": r[3]} for r in rows}


def zerotrac_cache_updated_at() -> int:
    """Return the most recent updated_at from the cache, or 0 if empty."""
    with _db() as conn:
        row = conn.execute("SELECT MAX(updated_at) FROM zerotrac_cache").fetchone()
        return int(row[0]) if row and row[0] else 0


def zerotrac_cache_upsert_all(entries: list[dict]):
    """Bulk upsert zerotrac entries. Each entry: {title_slug, rating, contest_slug, problem_index}."""
    now = int(time.time())
    with _db() as conn:
        conn.executemany(
            """INSERT INTO zerotrac_cache(title_slug, rating, contest_slug, problem_index, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(title_slug) DO UPDATE SET
                 rating=excluded.rating,
                 contest_slug=excluded.contest_slug,
                 problem_index=excluded.problem_index,
                 updated_at=excluded.updated_at""",
            [(e["title_slug"], e["rating"], e["contest_slug"], e["problem_index"], now) for e in entries],
        )
        conn.commit()
# ---- #schedule state helpers ----

def schedule_state_get() -> dict:
    """Return the pinned message ids and last successful sync time.

    The singleton row is seeded in db_init, so it always exists; ids are None
    until the messages have been created.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT week_message_id, month_message_id, last_synced_at FROM schedule_state WHERE id=1"
        ).fetchone()
        return {"week_message_id": row[0], "month_message_id": row[1], "last_synced_at": row[2]}


def schedule_set_message_ids(*, week_message_id: int | None = None, month_message_id: int | None = None):
    """Persist one or both message ids without clobbering the other.

    Pass only the id(s) that changed; a None argument leaves that column as-is.
    """
    with _db() as conn:
        conn.execute(
            """UPDATE schedule_state SET
                 week_message_id=COALESCE(?, week_message_id),
                 month_message_id=COALESCE(?, month_message_id)
               WHERE id=1""",
            (week_message_id, month_message_id),
        )
        conn.commit()


def schedule_set_last_synced(ts: int):
    with _db() as conn:
        conn.execute("UPDATE schedule_state SET last_synced_at=? WHERE id=1", (ts,))
        conn.commit()
