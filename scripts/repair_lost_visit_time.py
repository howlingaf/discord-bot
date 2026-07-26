"""One-off: credit voice sessions that a restart closed with zero time.

Before the heartbeat fix, _startup_fixups() closed any session still open at
boot with add_elapsed=False — so a user who was connected when the bot
restarted lost every minute since their last join. Those rows are recognisable
by seconds=0 (nothing was ever credited) with a real span between last_join_at
and left_at. This credits that span.

Run from the repo root:  uv run python scripts/repair_lost_visit_time.py [--apply]
Without --apply it only prints what it would change.
"""

import sys

sys.path.insert(0, ".")

from bot.database import _db  # noqa: E402

APPLY = "--apply" in sys.argv


def main():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, channel_id, last_join_at, left_at, seconds "
            "FROM voice_visits WHERE left_at IS NOT NULL AND seconds = 0 "
            "AND left_at - last_join_at >= 60"
        ).fetchall()

        if not rows:
            print("nothing to repair")
            return

        for vid, user_id, channel_id, last_join_at, left_at, _ in rows:
            span = left_at - last_join_at
            print(f"visit {vid}: user {user_id} room {channel_id} -> credit {span // 60} min")
            if APPLY:
                conn.execute("UPDATE voice_visits SET seconds=? WHERE id=?", (span, vid))

        if APPLY:
            conn.commit()
            print(f"repaired {len(rows)} row(s)")
        else:
            print(f"\n{len(rows)} row(s) would change — re-run with --apply")


if __name__ == "__main__":
    main()
