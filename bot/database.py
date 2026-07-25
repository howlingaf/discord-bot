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

        # ---- Fair-access cooldown system ----
        # Singleton runtime state: when the tracked rooms last all went empty
        # (drives the session-window reset) and the pinned admin panel message.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS fairaccess_state (
          id               INTEGER PRIMARY KEY CHECK (id = 1),
          all_empty_since  INTEGER,
          panel_message_id INTEGER
        )
        """)
        conn.execute("INSERT OR IGNORE INTO fairaccess_state(id) VALUES(1)")

        # Exempt users: pure internal state, no Discord artifact of any kind.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS fairaccess_whitelist (
          user_id  INTEGER PRIMARY KEY,
          added_by INTEGER NOT NULL,
          added_at INTEGER NOT NULL
        )
        """)

        # One row per user per session window; doubles as the visitor feed.
        # room_seconds is a JSON object {channel_id: seconds}. status: 'open'
        # while the window can still accrue, then 'ok' | 'exempt' | 'flagged'.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS fairaccess_windows (
          id                   INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id              INTEGER NOT NULL,
          started_at           INTEGER NOT NULL,
          last_activity_at     INTEGER NOT NULL,
          last_join_at         INTEGER,
          last_join_channel_id INTEGER,
          room_seconds         TEXT NOT NULL DEFAULT '{}',
          status               TEXT NOT NULL DEFAULT 'open'
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_windows_status ON fairaccess_windows(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_windows_activity ON fairaccess_windows(last_activity_at)")

        # Historical rows are kept forever (they back the visitor feed's flagged
        # entries). Active = released_at, expired_at both NULL and not yet due.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS fairaccess_cooldowns (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id      INTEGER NOT NULL,
          applied_at   INTEGER NOT NULL,
          expires_at   INTEGER NOT NULL,
          room_seconds TEXT NOT NULL DEFAULT '{}',
          applied_by   INTEGER,
          released_by  INTEGER,
          released_at  INTEGER,
          expired_at   INTEGER
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fa_cooldowns_user ON fairaccess_cooldowns(user_id)")

        # ---- Voice visit log (who was in which voice channel, how long) ----
        # One row per stint; sub-5-min rejoins to the same channel resume the
        # row instead of opening a new one. Feeds the admin panel's sessions
        # section. Fully decoupled from fair-access tallies.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_visits (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id      INTEGER NOT NULL,
          channel_id   INTEGER NOT NULL,
          started_at   INTEGER NOT NULL,
          last_join_at INTEGER NOT NULL,
          left_at      INTEGER,
          seconds      INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_visits_open ON voice_visits(user_id) WHERE left_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_visits_recency ON voice_visits(left_at, last_join_at)")

        # ---- Temporary voice channel names (/rename) ----
        # A row exists only while a channel carries a custom name; default_name
        # is the name it had before the first rename, and the row is deleted
        # once the channel empties and the default is restored.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_channel_names (
          channel_id   INTEGER PRIMARY KEY,
          default_name TEXT NOT NULL,
          custom_name  TEXT NOT NULL,
          set_by       INTEGER NOT NULL,
          set_at       INTEGER NOT NULL
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


# ---- Fair-access helpers ----

def fairaccess_state_get() -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT all_empty_since, panel_message_id FROM fairaccess_state WHERE id=1"
        ).fetchone()
        return {"all_empty_since": row[0], "panel_message_id": row[1]}


def fairaccess_set_all_empty_since(ts: int | None):
    with _db() as conn:
        conn.execute("UPDATE fairaccess_state SET all_empty_since=? WHERE id=1", (ts,))
        conn.commit()


def fairaccess_set_panel_message(message_id: int):
    with _db() as conn:
        conn.execute("UPDATE fairaccess_state SET panel_message_id=? WHERE id=1", (message_id,))
        conn.commit()


def fairaccess_whitelist_add(user_id: int, added_by: int) -> bool:
    """True if newly added, False if already present."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO fairaccess_whitelist(user_id, added_by, added_at) VALUES(?,?,?)",
            (user_id, added_by, int(time.time())),
        )
        conn.commit()
        return cur.rowcount > 0


def fairaccess_whitelist_remove(user_id: int) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM fairaccess_whitelist WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0


def fairaccess_whitelist_has(user_id: int) -> bool:
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM fairaccess_whitelist WHERE user_id=?", (user_id,)
        ).fetchone() is not None


def fairaccess_whitelist_all() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT user_id, added_by, added_at FROM fairaccess_whitelist ORDER BY added_at"
        ).fetchall()
        return [{"user_id": r[0], "added_by": r[1], "added_at": r[2]} for r in rows]


def _fa_window_row(r) -> dict:
    return {
        "id": r[0], "user_id": r[1], "started_at": r[2], "last_activity_at": r[3],
        "last_join_at": r[4], "last_join_channel_id": r[5],
        "room_seconds": r[6], "status": r[7],
    }


_FA_WINDOW_COLS = ("id, user_id, started_at, last_activity_at, last_join_at, "
                   "last_join_channel_id, room_seconds, status")


def fairaccess_window_open_for(user_id: int) -> dict | None:
    with _db() as conn:
        r = conn.execute(
            f"SELECT {_FA_WINDOW_COLS} FROM fairaccess_windows WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchone()
        return _fa_window_row(r) if r else None


def fairaccess_window_create(user_id: int, now: int) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO fairaccess_windows(user_id, started_at, last_activity_at) VALUES(?,?,?)",
            (user_id, now, now),
        )
        conn.commit()
        return cur.lastrowid


def fairaccess_window_update(window_id: int, **fields):
    """Partial update; pass only the columns to change (None writes NULL)."""
    assert fields
    cols = ", ".join(f"{k}=?" for k in fields)
    with _db() as conn:
        conn.execute(f"UPDATE fairaccess_windows SET {cols} WHERE id=?",
                     (*fields.values(), window_id))
        conn.commit()


def fairaccess_windows_open() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            f"SELECT {_FA_WINDOW_COLS} FROM fairaccess_windows WHERE status='open'"
        ).fetchall()
        return [_fa_window_row(r) for r in rows]


def fairaccess_windows_recent(limit: int = 15) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            f"SELECT {_FA_WINDOW_COLS} FROM fairaccess_windows "
            "ORDER BY last_activity_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [_fa_window_row(r) for r in rows]


def _fa_cooldown_row(r) -> dict:
    return {
        "id": r[0], "user_id": r[1], "applied_at": r[2], "expires_at": r[3],
        "room_seconds": r[4], "applied_by": r[5], "released_by": r[6],
        "released_at": r[7], "expired_at": r[8],
    }


_FA_COOLDOWN_COLS = ("id, user_id, applied_at, expires_at, room_seconds, "
                     "applied_by, released_by, released_at, expired_at")


def fairaccess_cooldown_create(user_id: int, applied_at: int, expires_at: int,
                               room_seconds: str, applied_by: int | None) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO fairaccess_cooldowns(user_id, applied_at, expires_at, room_seconds, applied_by) "
            "VALUES(?,?,?,?,?)",
            (user_id, applied_at, expires_at, room_seconds, applied_by),
        )
        conn.commit()
        return cur.lastrowid


def fairaccess_cooldown_active_for(user_id: int) -> dict | None:
    with _db() as conn:
        r = conn.execute(
            f"SELECT {_FA_COOLDOWN_COLS} FROM fairaccess_cooldowns "
            "WHERE user_id=? AND released_at IS NULL AND expired_at IS NULL AND expires_at>? ",
            (user_id, int(time.time())),
        ).fetchone()
        return _fa_cooldown_row(r) if r else None


def fairaccess_cooldowns_active() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            f"SELECT {_FA_COOLDOWN_COLS} FROM fairaccess_cooldowns "
            "WHERE released_at IS NULL AND expired_at IS NULL AND expires_at>?",
            (int(time.time()),),
        ).fetchall()
        return [_fa_cooldown_row(r) for r in rows]


def fairaccess_cooldowns_due() -> list[dict]:
    """Active-until-now cooldowns whose expiry has passed (needs overwrite removal)."""
    with _db() as conn:
        rows = conn.execute(
            f"SELECT {_FA_COOLDOWN_COLS} FROM fairaccess_cooldowns "
            "WHERE released_at IS NULL AND expired_at IS NULL AND expires_at<=?",
            (int(time.time()),),
        ).fetchall()
        return [_fa_cooldown_row(r) for r in rows]


def fairaccess_cooldown_mark_expired(cooldown_id: int, now: int):
    with _db() as conn:
        conn.execute("UPDATE fairaccess_cooldowns SET expired_at=? WHERE id=?", (now, cooldown_id))
        conn.commit()


def fairaccess_cooldown_release(cooldown_id: int, released_by: int, now: int):
    with _db() as conn:
        conn.execute(
            "UPDATE fairaccess_cooldowns SET released_by=?, released_at=? WHERE id=?",
            (released_by, now, cooldown_id),
        )
        conn.commit()


# ---- Voice visit log (general presence tracking, decoupled from cooldowns) ----

def voice_visit_open_for(user_id: int) -> dict | None:
    with _db() as conn:
        r = conn.execute(
            "SELECT id, user_id, channel_id, started_at, last_join_at, left_at, seconds "
            "FROM voice_visits WHERE user_id=? AND left_at IS NULL", (user_id,),
        ).fetchone()
        if not r:
            return None
        return {"id": r[0], "user_id": r[1], "channel_id": r[2], "started_at": r[3],
                "last_join_at": r[4], "left_at": r[5], "seconds": r[6]}


def voice_visit_recent_for(user_id: int, since: int) -> int | None:
    """Id of this user's most recent CLOSED session ending after `since` — the
    rejoin-merge target. Channel-agnostic: a session spans the tracked rooms."""
    with _db() as conn:
        r = conn.execute(
            "SELECT id FROM voice_visits WHERE user_id=? "
            "AND left_at IS NOT NULL AND left_at>=? ORDER BY left_at DESC LIMIT 1",
            (user_id, since),
        ).fetchone()
        return r[0] if r else None


def voice_visit_start(user_id: int, channel_id: int, now: int) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO voice_visits(user_id, channel_id, started_at, last_join_at, seconds) "
            "VALUES(?,?,?,?,0)", (user_id, channel_id, now, now),
        )
        conn.commit()
        return cur.lastrowid


def voice_visit_resume(visit_id: int, now: int):
    with _db() as conn:
        conn.execute("UPDATE voice_visits SET left_at=NULL, last_join_at=? WHERE id=?",
                     (now, visit_id))
        conn.commit()


def voice_visit_close(visit_id: int, now: int, *, add_elapsed: bool = True):
    with _db() as conn:
        if add_elapsed:
            conn.execute(
                "UPDATE voice_visits SET seconds=seconds+(?-last_join_at), left_at=? WHERE id=?",
                (now, now, visit_id))
        else:
            conn.execute("UPDATE voice_visits SET left_at=? WHERE id=?", (now, visit_id))
        conn.commit()


def voice_visits_open() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, channel_id, started_at, last_join_at, left_at, seconds "
            "FROM voice_visits WHERE left_at IS NULL").fetchall()
        return [{"id": r[0], "user_id": r[1], "channel_id": r[2], "started_at": r[3],
                 "last_join_at": r[4], "left_at": r[5], "seconds": r[6]} for r in rows]


def _voice_name_row(r) -> dict:
    return {"channel_id": r[0], "default_name": r[1], "custom_name": r[2],
            "set_by": r[3], "set_at": r[4]}


def voice_name_get(channel_id: int) -> dict | None:
    with _db() as conn:
        r = conn.execute(
            "SELECT channel_id, default_name, custom_name, set_by, set_at "
            "FROM voice_channel_names WHERE channel_id=?", (channel_id,),
        ).fetchone()
        return _voice_name_row(r) if r else None


def voice_names_all() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT channel_id, default_name, custom_name, set_by, set_at "
            "FROM voice_channel_names").fetchall()
        return [_voice_name_row(r) for r in rows]


def voice_name_set(channel_id: int, default_name: str, custom_name: str, set_by: int, now: int):
    """Record an active override. The stored default_name is never overwritten
    by a second rename — the first one captured the real default."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO voice_channel_names(channel_id, default_name, custom_name, set_by, set_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
            "custom_name=excluded.custom_name, set_by=excluded.set_by, set_at=excluded.set_at",
            (channel_id, default_name, custom_name, set_by, now),
        )
        conn.commit()


def voice_name_clear(channel_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM voice_channel_names WHERE channel_id=?", (channel_id,))
        conn.commit()


def voice_time_totals(channel_ids: list[int], now: int, limit: int = 15) -> list[dict]:
    """One row per user: their all-time total seconds in `channel_ids`, longest
    first. Open sessions include the time accrued since the last join, so
    someone in a room right now keeps climbing."""
    if not channel_ids:
        return []
    marks = ",".join("?" * len(channel_ids))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT user_id, SUM(seconds) + SUM(CASE WHEN left_at IS NULL "
            f"THEN MAX(0, ?-last_join_at) ELSE 0 END) AS total "
            f"FROM voice_visits WHERE channel_id IN ({marks}) "
            f"GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (now, *channel_ids, limit),
        ).fetchall()
        return [{"user_id": r[0], "seconds": r[1] or 0} for r in rows]
