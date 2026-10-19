"""Database layer: every read and write to mail.db lives here. Other files only call these
functions and never write SQL themselves.

Three tables:
    emails    The emails and their status (new → review → todo / discarded)
    analyses  AI analysis results, at most one per email (the error field records why an analysis failed)
    todos     To-do items; "done" is stored in this table's done field, not as an email status

Notes:
    - Every function opens a new connection and closes it when finished. Gradio calls these from
      different threads, and sqlite3 forbids sharing one connection across threads by default.
    - All variables are passed in with ? placeholders and never concatenated into the SQL.
    - The database path is the module variable DB_PATH; tests can point it at a temporary file:
      db.DB_PATH = "some_path.db"
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# ---------------------------------------------------------------- constants

# Email statuses
NEW = "new"                # Just fetched, not analyzed yet
REVIEW = "review"          # Analyzed, waiting for my decision
TODO = "todo"              # Added to the to-do list
DISCARDED = "discarded"    # Discarded (can be restored to review)
ALL_STATUSES = (NEW, REVIEW, TODO, DISCARDED)

# To-do sort orders
SORT_DEADLINE = "deadline"
SORT_PRIORITY = "priority"

# Default database file: mail.db in the same folder as db.py
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail.db")

# Column names of the two tables, used to split a joined row into its "email" and "analysis" parts
EMAIL_COLUMNS = ("key", "account", "msg_id", "thread_id", "internal_ms", "received_at",
                 "sender", "to_addr", "cc_addr", "subject", "labels", "attachments",
                 "snippet", "body", "status", "status_changed_at", "fetched_at")
ANALYSIS_COLUMNS = ("email_key", "category", "priority", "needs_action", "action", "deadline",
                    "summary", "reason", "suspicious", "model", "prompt_version", "error",
                    "analyzed_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    key               TEXT PRIMARY KEY,      -- "alias:Gmail message ID"
    account           TEXT NOT NULL,         -- account alias
    msg_id            TEXT NOT NULL,         -- Gmail message ID
    thread_id         TEXT,
    internal_ms       INTEGER,               -- Gmail internal receive time (millisecond timestamp)
    received_at       TEXT,                  -- receive time (local time, ISO format)
    sender            TEXT,
    to_addr           TEXT,
    cc_addr           TEXT,
    subject           TEXT,
    labels            TEXT,                  -- JSON text (list)
    attachments       TEXT,                  -- JSON text (list of attachment file names)
    snippet           TEXT,
    body              TEXT,
    status            TEXT NOT NULL,
    status_changed_at TEXT,
    fetched_at        TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    email_key      TEXT PRIMARY KEY REFERENCES emails(key),
    category       TEXT,
    priority       INTEGER CHECK (priority BETWEEN 1 AND 4),
    needs_action   INTEGER CHECK (needs_action IN (0, 1)),
    action         TEXT,
    deadline       TEXT,                     -- YYYY-MM-DD or NULL
    summary        TEXT,
    reason         TEXT,
    suspicious     INTEGER CHECK (suspicious IN (0, 1)),
    model          TEXT,
    prompt_version TEXT,
    error          TEXT,                     -- failure reason; NULL on success
    analyzed_at    TEXT
);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_key  TEXT REFERENCES emails(key),
    account    TEXT,
    title      TEXT NOT NULL,
    deadline   TEXT,                         -- YYYY-MM-DD or NULL
    priority   INTEGER CHECK (priority BETWEEN 1 AND 4),
    note       TEXT,
    done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    created_at TEXT,
    done_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_status  ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_account ON emails(account);
CREATE INDEX IF NOT EXISTS idx_todos_done     ON todos(done);
"""

# Joined query: email + analysis (LEFT JOIN, so emails without an analysis are returned too)
_SELECT_EMAIL = "SELECT e.*, a.* FROM emails AS e LEFT JOIN analyses AS a ON a.email_key = e.key"

# Sort orders for list_emails (fixed SQL fragments)
_EMAIL_ORDER = {
    # Emails without a priority (failed analysis) first, as a reminder to check them by hand;
    # then by priority ascending, then by receive time descending
    REVIEW: "a.priority IS NOT NULL, a.priority ASC, e.internal_ms DESC",
    # Most recently discarded first
    DISCARDED: "e.status_changed_at DESC, e.internal_ms DESC",
}
_EMAIL_ORDER_DEFAULT = "e.internal_ms DESC"

# Sort orders for list_todos (fixed SQL fragments); items without a deadline/priority go last
_TODO_ORDER = {
    SORT_DEADLINE: "deadline IS NULL, deadline ASC, priority IS NULL, priority ASC, id ASC",
    SORT_PRIORITY: "priority IS NULL, priority ASC, deadline IS NULL, deadline ASC, id ASC",
}


# ---------------------------------------------------------------- internal helpers

@contextmanager
def _connect():
    """Open a new connection and close it when the with block exits.

    Usage: with _connect() as conn, conn:
        The first with closes the connection; the second "with conn" handles the transaction
        (commits on normal exit, rolls back on error). Read-only queries can use just
        with _connect() as conn:
    """
    conn = sqlite3.connect(DB_PATH)   # Read DB_PATH at call time, so a path changed by tests takes effect
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now():
    """Current local time in ISO format, to the second."""
    return datetime.now().isoformat(timespec="seconds")


def _check_status(status):
    """The status must be one of the four constants."""
    if status not in ALL_STATUSES:
        raise ValueError(f"Unknown email status: {status!r}")


def _check_date(value):
    """The date must be YYYY-MM-DD or empty; an empty string becomes None. Returns the normalized value."""
    if value in (None, ""):
        return None
    datetime.strptime(value, "%Y-%m-%d")   # Raises ValueError if the format is wrong
    return value


def _to_flag(value):
    """Convert True/False to 1/0; None stays None (a failed analysis has no value)."""
    return None if value is None else int(bool(value))


def _to_int(value):
    """Convert to an integer; None stays None."""
    return None if value is None else int(value)


def _unique(items):
    """Remove duplicates while keeping the original order; returns a list."""
    return list(dict.fromkeys(items))


def _row_to_email(row):
    """Turn one row of the joined query into a plain dict; the analysis goes under the "analysis"
    key, or None if there is no analysis."""
    email = {col: row[col] for col in EMAIL_COLUMNS}
    email["labels"] = json.loads(email["labels"] or "[]")
    email["attachments"] = json.loads(email["attachments"] or "[]")
    has_analysis = row["email_key"] is not None
    email["analysis"] = {col: row[col] for col in ANALYSIS_COLUMNS} if has_analysis else None
    return email


def _todo_title(row):
    """To-do title: the analysis action by default; the email subject when the action is empty."""
    action = (row["action"] or "").strip()
    return action or (row["subject"] or "")


# ---------------------------------------------------------------- initialization

def init_db():
    """Create the tables and indexes. Safe to run repeatedly; never clears existing data."""
    with _connect() as conn, conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- emails

def email_exists(key):
    """Whether this email is already in the database."""
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM emails WHERE key = ?", (key,)).fetchone()
    return row is not None


def insert_email(record):
    """Save one email (record is the dict returned by fetch_mail.fetch_one()).

    Ignored if it already exists; a new email gets status new. Returns whether a row was added.
    """
    now = _now()
    values = (
        record["key"], record["account"], record["id"], record.get("thread_id"),
        record.get("internal_ms"), record.get("received_at"),
        record.get("from"), record.get("to"), record.get("cc"), record.get("subject"),
        json.dumps(record.get("labels") or [], ensure_ascii=False),
        json.dumps(record.get("attachments") or [], ensure_ascii=False),
        record.get("snippet"), record.get("body"),
        NEW, now, now,
    )
    with _connect() as conn, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO emails (key, account, msg_id, thread_id, internal_ms, "
            "received_at, sender, to_addr, cc_addr, subject, labels, attachments, snippet, "
            "body, status, status_changed_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return cur.rowcount == 1


def latest_internal_ms(account):
    """internal_ms of this account's newest email in the database; None if it has no emails."""
    with _connect() as conn:
        row = conn.execute("SELECT MAX(internal_ms) AS m FROM emails WHERE account = ?",
                           (account,)).fetchone()
    return row["m"]


def list_emails(status, account=None, priority=None, limit=None):
    """List emails with the given status as a list of dicts, each with "analysis" (None if not analyzed).

    Sort order:
        review     no priority (failed analysis) first, then priority ascending, receive time descending
        discarded  discard time (status_changed_at) descending
        others     receive time descending
    account / priority / limit of None mean no filter and no limit.
    """
    _check_status(status)
    # Only the fixed SQL fragments defined above are concatenated; every variable goes into params via ?
    sql = _SELECT_EMAIL + " WHERE e.status = ?"
    params = [status]
    if account is not None:
        sql += " AND e.account = ?"
        params.append(account)
    if priority is not None:
        sql += " AND a.priority = ?"
        params.append(int(priority))
    sql += " ORDER BY " + _EMAIL_ORDER.get(status, _EMAIL_ORDER_DEFAULT)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_email(r) for r in rows]


def get_email(key):
    """Return one email with its analysis (the "analysis" key); None if the email does not exist."""
    with _connect() as conn:
        row = conn.execute(_SELECT_EMAIL + " WHERE e.key = ?", (key,)).fetchone()
    return _row_to_email(row) if row else None


def set_status(keys, status):
    """Change the status of several emails and update status_changed_at. Returns how many actually
    changed (emails already in that status are not counted)."""
    _check_status(status)
    keys = _unique(keys)
    if not keys:
        return 0
    now = _now()
    with _connect() as conn, conn:
        cur = conn.executemany(
            "UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ? AND status != ?",
            [(status, now, key, status) for key in keys],
        )
        return cur.rowcount


def reset_to_new(keys):
    """Set emails back to new and delete their analyses (used to re-run the evaluation after
    changing the prompt). Returns how many changed.

    Note: only emails and analyses are touched; existing to-dos are not deleted.
    """
    keys = _unique(keys)
    if not keys:
        return 0
    now = _now()
    with _connect() as conn, conn:
        conn.executemany("DELETE FROM analyses WHERE email_key = ?", [(key,) for key in keys])
        cur = conn.executemany(
            "UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ? AND status != ?",
            [(NEW, now, key, NEW) for key in keys],
        )
        return cur.rowcount


# ---------------------------------------------------------------- analyses

def save_analysis(key, result, model, prompt_version, error=None):
    """Write or overwrite an email's analysis and set the email status to review (one transaction).

    result is the analysis dict with keys category, priority, needs_action, action, deadline,
    summary, reason, suspicious; pass None when the analysis failed and put the reason in error.
    Raises KeyError if the email does not exist, sqlite3.IntegrityError if priority is not 1–4,
    and ValueError if deadline is not YYYY-MM-DD. On any error the whole operation is rolled back.
    """
    result = result or {}
    values = (
        key,
        result.get("category"),
        _to_int(result.get("priority")),
        _to_flag(result.get("needs_action")),
        result.get("action"),
        _check_date(result.get("deadline")),
        result.get("summary"),
        result.get("reason"),
        _to_flag(result.get("suspicious")),
        model,
        prompt_version,
        error,
        _now(),
    )
    with _connect() as conn, conn:
        cur = conn.execute("UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ?",
                           (REVIEW, _now(), key))
        if cur.rowcount == 0:
            raise KeyError(f"Email does not exist: {key}")
        conn.execute(
            "INSERT OR REPLACE INTO analyses (email_key, category, priority, needs_action, "
            "action, deadline, summary, reason, suspicious, model, prompt_version, error, "
            "analyzed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


# ---------------------------------------------------------------- to-dos

def add_todos(keys):
    """Create a to-do from each email's analysis and set the email status to todo (one transaction).
    Returns how many were created.

    The title defaults to the action, or the email subject when the action is empty; deadline and
    priority are copied from the analysis. Emails already in todo, or that do not exist, are
    skipped, so no duplicates are created.
    """
    keys = _unique(keys)
    now = _now()
    created = 0
    with _connect() as conn, conn:
        for key in keys:
            # Change the status first: if the email is already todo or does not exist,
            # nothing changes (rowcount is 0) and we skip it
            cur = conn.execute(
                "UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ? AND status != ?",
                (TODO, now, key, TODO),
            )
            if cur.rowcount == 0:
                continue
            row = conn.execute(
                "SELECT e.account, e.subject, a.action, a.deadline, a.priority "
                "FROM emails AS e LEFT JOIN analyses AS a ON a.email_key = e.key "
                "WHERE e.key = ?",
                (key,),
            ).fetchone()
            conn.execute(
                "INSERT INTO todos (email_key, account, title, deadline, priority, note, "
                "done, created_at, done_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL)",
                (key, row["account"], _todo_title(row), row["deadline"], row["priority"],
                 "", now),
            )
            created += 1
    return created


def list_todos(done=False, account=None, sort=SORT_DEADLINE, limit=None):
    """List to-dos as a list of dicts.

    done: False for open items, True for completed ones.
    sort: "deadline" sorts by deadline ascending (items without a deadline last); "priority" sorts
    by priority ascending (items without a priority last).
    """
    if sort not in _TODO_ORDER:
        raise ValueError(f"Unknown sort order: {sort!r}")
    # Only fixed SQL fragments are concatenated; variables are passed with ?
    sql = "SELECT * FROM todos WHERE done = ?"
    params = [int(bool(done))]
    if account is not None:
        sql += " AND account = ?"
        params.append(account)
    sql += " ORDER BY " + _TODO_ORDER[sort]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_todo(todo_id):
    """Return one to-do; None if it does not exist."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return dict(row) if row else None


def update_todo(todo_id, title, deadline, note):
    """Change a to-do's title, deadline (YYYY-MM-DD or empty) and note. Returns whether it was found."""
    deadline = _check_date(deadline)
    with _connect() as conn, conn:
        cur = conn.execute("UPDATE todos SET title = ?, deadline = ?, note = ? WHERE id = ?",
                           (title, deadline, note, todo_id))
        return cur.rowcount == 1


def mark_done(ids):
    """Mark several to-dos as done and record the completion time. Returns how many changed
    (items already done are not counted)."""
    ids = _unique(ids)
    if not ids:
        return 0
    now = _now()
    with _connect() as conn, conn:
        cur = conn.executemany("UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND done = 0",
                               [(now, i) for i in ids])
        return cur.rowcount


def undo_done(ids):
    """Undo completion for several to-dos (back to open, completion time cleared). Returns how many changed."""
    ids = _unique(ids)
    if not ids:
        return 0
    with _connect() as conn, conn:
        cur = conn.executemany("UPDATE todos SET done = 0, done_at = NULL WHERE id = ? AND done = 1",
                               [(i,) for i in ids])
        return cur.rowcount


def delete_todos(ids):
    """Delete several to-dos and move their emails from todo to discarded (one transaction).
    Returns how many were deleted.

    Once in "Discarded", an email can be restored to review; adding it to the to-do list again
    creates a new to-do. If a to-do has no source email, or the email is no longer in todo,
    only the to-do is deleted and the email is left alone.
    """
    ids = _unique(ids)
    if not ids:
        return 0
    now = _now()
    deleted = 0
    with _connect() as conn, conn:
        for todo_id in ids:
            row = conn.execute("SELECT email_key FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if row is None:                      # The to-do no longer exists; skip it
                continue
            conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            deleted += 1
            if row["email_key"] is not None:
                conn.execute(
                    "UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ? AND status = ?",
                    (DISCARDED, now, row["email_key"], TODO),
                )
    return deleted


# ---------------------------------------------------------------- statistics

def count_by_status():
    """Number of emails per account per status, e.g.
    {"work": {"new": 3, "review": 5, "todo": 1, "discarded": 0}}."""
    with _connect() as conn:
        rows = conn.execute("SELECT account, status, COUNT(*) AS n FROM emails "
                            "GROUP BY account, status ORDER BY account").fetchall()
    counts = {}
    for r in rows:
        counts.setdefault(r["account"], {s: 0 for s in ALL_STATUSES})[r["status"]] = r["n"]
    return counts
