"""One-off: drop the contest-post bookkeeping for retired contest forums.

Every row in leetcode_contest_posts points at a thread inside a weekly/biweekly
contest forum, and leetcode_contest_state names the newest thread per type. Once
those forums are deleted the rows reference nothing — and an unrated row would
make the ratings updater try to edit a thread that no longer exists.

Run from the repo root:  uv run python scripts/purge_contest_posts.py [--apply]
Without --apply it only prints what it would delete.
"""

import sys

sys.path.insert(0, ".")

from bot.database import _db  # noqa: E402

APPLY = "--apply" in sys.argv


def main():
    with _db() as conn:
        posts = conn.execute(
            "SELECT contest_type, COUNT(*), SUM(rated=0) FROM leetcode_contest_posts "
            "GROUP BY contest_type").fetchall()
        state = conn.execute(
            "SELECT contest_type, last_title_slug FROM leetcode_contest_state").fetchall()

        if not posts and not state:
            print("nothing to purge")
            return

        for ctype, count, unrated in posts:
            print(f"leetcode_contest_posts: {ctype} — {count} row(s), {unrated} unrated")
        for ctype, slug in state:
            print(f"leetcode_contest_state: {ctype} — {slug}")

        if not APPLY:
            print("\ndry run — re-run with --apply to delete")
            return

        posts_deleted = conn.execute("DELETE FROM leetcode_contest_posts").rowcount
        state_deleted = conn.execute("DELETE FROM leetcode_contest_state").rowcount
        conn.commit()
        print(f"\ndeleted {posts_deleted} contest post row(s), {state_deleted} state row(s)")


if __name__ == "__main__":
    main()
