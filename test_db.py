"""Tests for db.py: run the whole flow against a temporary database file, check everything with
assert, and delete the temporary file afterwards.

Usage (run inside the project folder):
    python test_db.py
Never touches the real mail.db.
"""

import os
import shutil
import sqlite3
import tempfile

import db


def make_record(alias, msg_id, internal_ms, subject):
    """Build a fake email with the same fields as fetch_mail.fetch_one() returns."""
    return {
        "key": f"{alias}:{msg_id}",
        "account": alias,
        "id": msg_id,
        "thread_id": "t-" + msg_id,
        "received_at": "2026-10-14T09:00:00",
        "internal_ms": internal_ms,
        "from": "Test <test@example.com>",
        "to": "me@example.com",
        "cc": "",
        "subject": subject,
        "labels": ["INBOX", "UNREAD"],
        "snippet": "Snippet",
        "attachments": ["quote.pdf"],
        "body": "Test body",
    }


GOOD_RESULT = {
    "category": "Job search", "priority": 2, "needs_action": True, "action": "Reply with a quote",
    "deadline": "2026-10-20", "summary": "They want a quote", "reason": "Has a clear deadline",
    "suspicious": False,
}


def run_tests():
    # 1. Initializing twice does not fail
    db.init_db()
    db.init_db()

    # 2. Inserting the same record twice stores only one row
    a = make_record("work", "m1", 1000, "Please send a quote")
    assert db.insert_email(a) is True
    assert db.insert_email(a) is False
    assert db.email_exists(a["key"])
    assert not db.email_exists("work:missing")
    news = db.list_emails(db.NEW)
    assert len(news) == 1
    assert news[0]["labels"] == ["INBOX", "UNREAD"] and news[0]["attachments"] == ["quote.pdf"]
    assert news[0]["sender"] == a["from"] and news[0]["analysis"] is None

    b = make_record("work", "m2", 3000, "Unclear email")
    c = make_record("home", "m3", 2000, "Email with no action item")
    db.insert_email(b)
    db.insert_email(c)
    assert db.latest_internal_ms("work") == 3000
    assert db.latest_internal_ms("no-such-account") is None

    # 3. After saving an analysis the status is review; failed analyses are returned too, and come first
    db.save_analysis(a["key"], GOOD_RESULT, "gemini-test", "v1")
    db.save_analysis(b["key"], None, "gemini-test", "v1", error="Timeout")
    db.save_analysis(c["key"], {**GOOD_RESULT, "action": "", "priority": 4, "deadline": None},
                     "gemini-test", "v1")
    got = db.get_email(a["key"])
    assert got["status"] == db.REVIEW
    assert got["analysis"]["priority"] == 2 and got["analysis"]["needs_action"] == 1
    assert got["analysis"]["error"] is None
    review = db.list_emails(db.REVIEW)
    assert [e["key"] for e in review] == [b["key"], a["key"], c["key"]]   # Failed (no priority) first
    assert review[0]["analysis"]["error"] == "Timeout"
    assert [e["key"] for e in db.list_emails(db.REVIEW, account="home")] == [c["key"]]
    assert [e["key"] for e in db.list_emails(db.REVIEW, priority=2)] == [a["key"]]
    assert len(db.list_emails(db.REVIEW, limit=1)) == 1

    # Overwriting an analysis: saving again keeps a single row
    db.save_analysis(a["key"], GOOD_RESULT, "gemini-test", "v1")
    assert db.get_email(a["key"])["analysis"]["model"] == "gemini-test"

    # A missing email or an invalid priority must raise, and leave no partial data behind
    try:
        db.save_analysis("work:missing", GOOD_RESULT, "m", "v1")
        assert False, "should raise KeyError"
    except KeyError:
        pass
    try:
        db.save_analysis(a["key"], {**GOOD_RESULT, "priority": 9}, "other-model", "v1")
        assert False, "should raise IntegrityError"
    except sqlite3.IntegrityError:
        pass
    assert db.get_email(a["key"])["analysis"]["model"] == "gemini-test"   # Rolled back; old analysis kept

    # 4. After add_todos the status is todo and the to-do exists; adding again creates no duplicate
    assert db.add_todos([a["key"], c["key"]]) == 2
    assert db.get_email(a["key"])["status"] == db.TODO
    assert db.add_todos([a["key"]]) == 0
    todos = db.list_todos()
    assert len(todos) == 2
    todo_a = next(t for t in todos if t["email_key"] == a["key"])
    todo_c = next(t for t in todos if t["email_key"] == c["key"])
    assert todo_a["title"] == "Reply with a quote"                  # Title comes from the action
    assert todo_a["deadline"] == "2026-10-20" and todo_a["priority"] == 2
    assert todo_c["title"] == "Email with no action item"           # Subject when the action is empty
    assert [t["id"] for t in todos] == [todo_a["id"], todo_c["id"]]   # No deadline goes last
    assert [t["id"] for t in db.list_todos(sort="priority")] == [todo_a["id"], todo_c["id"]]
    assert [t["id"] for t in db.list_todos(account="home")] == [todo_c["id"]]

    assert db.update_todo(todo_c["id"], "Edited title", "2026-10-15", "Note") is True
    edited = db.get_todo(todo_c["id"])
    assert edited["title"] == "Edited title" and edited["deadline"] == "2026-10-15"
    assert [t["id"] for t in db.list_todos()] == [todo_c["id"], todo_a["id"]]   # Earlier deadline first

    # 5. mark_done / undo_done
    assert db.mark_done([todo_a["id"]]) == 1
    assert db.mark_done([todo_a["id"]]) == 0                  # Already done; not counted again
    assert [t["id"] for t in db.list_todos(done=False)] == [todo_c["id"]]
    done_list = db.list_todos(done=True)
    assert [t["id"] for t in done_list] == [todo_a["id"]] and done_list[0]["done_at"]
    assert db.undo_done([todo_a["id"]]) == 1
    restored = db.get_todo(todo_a["id"])
    assert restored["done"] == 0 and restored["done_at"] is None

    # Deleting a to-do: the to-do disappears and its email moves to discarded
    assert db.delete_todos([todo_c["id"]]) == 1
    assert db.get_todo(todo_c["id"]) is None
    assert db.get_email(c["key"])["status"] == db.DISCARDED
    assert db.delete_todos([todo_c["id"]]) == 0                # Already deleted; not counted again

    # After restoring to review it can be added again, creating a new to-do; deleting it again
    # moves the email back to discarded
    assert db.set_status([c["key"]], db.REVIEW) == 1
    assert db.add_todos([c["key"]]) == 1
    todo_c2 = next(t for t in db.list_todos() if t["email_key"] == c["key"])
    assert todo_c2["id"] != todo_c["id"]
    assert db.delete_todos([todo_c2["id"]]) == 1
    assert db.get_email(c["key"])["status"] == db.DISCARDED

    # 6. Discard and restore (c is already in discarded)
    assert db.set_status([b["key"]], db.DISCARDED) == 1
    assert db.set_status([b["key"]], db.DISCARDED) == 0       # Already in that status; not counted
    assert [e["key"] for e in db.list_emails(db.DISCARDED)] == [b["key"], c["key"]]
    assert db.set_status([b["key"]], db.REVIEW) == 1
    assert db.get_email(b["key"])["status"] == db.REVIEW
    assert [e["key"] for e in db.list_emails(db.DISCARDED)] == [c["key"]]

    # Statistics
    counts = db.count_by_status()
    assert counts["work"] == {db.NEW: 0, db.REVIEW: 1, db.TODO: 1, db.DISCARDED: 0}
    assert counts["home"] == {db.NEW: 0, db.REVIEW: 0, db.TODO: 0, db.DISCARDED: 1}

    # 7. reset_to_new: status goes back to new and the analysis is deleted
    assert db.reset_to_new([b["key"]]) == 1
    got_b = db.get_email(b["key"])
    assert got_b["status"] == db.NEW and got_b["analysis"] is None
    assert [e["key"] for e in db.list_emails(db.NEW)] == [b["key"]]


def main():
    tmp_dir = tempfile.mkdtemp(prefix="mail_test_")
    original_path = db.DB_PATH
    db.DB_PATH = os.path.join(tmp_dir, "test_mail.db")
    try:
        run_tests()
        print("All db.py tests passed")
    finally:
        db.DB_PATH = original_path
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
