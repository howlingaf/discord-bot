"""One-off: delete a voice_visits row by id.

Used for rows the event stream corrupted — e.g. a session whose close was
swallowed by a crashing handler, leaving a span that was never really observed.

Run from the repo root:  uv run python scripts/drop_visit.py <id> [--apply]
Without --apply it only prints what it would delete.
"""

import sys

sys.path.insert(0, ".")

from bot.database import _db  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if len(args) != 1 or not args[0].isdigit():
        print(__doc__)
        sys.exit(1)
    vid = int(args[0])
    apply = "--apply" in sys.argv

    with _db() as conn:
        row = conn.execute(
            "SELECT id, user_id, channel_id, last_join_at, left_at, seconds "
            "FROM voice_visits WHERE id=?", (vid,),
        ).fetchone()
        if not row:
            print(f"visit {vid} not found")
            return
        print(f"visit {row[0]}: user {row[1]} room {row[2]} "
              f"last_join={row[3]} left={row[4]} seconds={row[5]} ({row[5] // 60} min)")
        if apply:
            conn.execute("DELETE FROM voice_visits WHERE id=?", (vid,))
            conn.commit()
            print("deleted")
        else:
            print("\nre-run with --apply to delete")


if __name__ == "__main__":
    main()
