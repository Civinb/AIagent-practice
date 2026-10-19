"""数据库层：所有对 mail.db 的读写都集中在这里，其他文件只调用这里的函数，不直接写 SQL。

三张表：
    emails    邮件本身和它的状态（new → review → todo / discarded）
    analyses  AI 分析结果，一封邮件最多一条（分析失败时 error 字段记录原因）
    todos     待办事项；"已完成"记在这里的 done 字段，不是邮件状态

使用注意：
    - 每个函数内部新开连接、用完关闭。Gradio 会在不同线程调用，sqlite3 默认禁止跨线程共用同一连接。
    - 所有变量都用 ? 占位符传入，不拼接到 SQL 里。
    - 数据库路径是模块变量 DB_PATH，测试时可以改成临时文件：db.DB_PATH = "某个路径.db"
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# ---------------------------------------------------------------- 常量

# 邮件状态
NEW = "new"                # 刚拉取，还没分析
REVIEW = "review"          # 已分析，等我决定
TODO = "todo"              # 已加入待办
DISCARDED = "discarded"    # 已丢弃（可恢复为 review）
ALL_STATUSES = (NEW, REVIEW, TODO, DISCARDED)

# 待办排序方式
SORT_DEADLINE = "deadline"
SORT_PRIORITY = "priority"

# 默认数据库文件：与 db.py 同目录的 mail.db
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail.db")

# 两张表的列名，用来把联表查询的结果拆成"邮件"和"分析"两部分
EMAIL_COLUMNS = ("key", "account", "msg_id", "thread_id", "internal_ms", "received_at",
                 "sender", "to_addr", "cc_addr", "subject", "labels", "attachments",
                 "snippet", "body", "status", "status_changed_at", "fetched_at")
ANALYSIS_COLUMNS = ("email_key", "category", "priority", "needs_action", "action", "deadline",
                    "summary", "reason", "suspicious", "model", "prompt_version", "error",
                    "analyzed_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    key               TEXT PRIMARY KEY,      -- "别名:Gmail邮件ID"
    account           TEXT NOT NULL,         -- 账号别名
    msg_id            TEXT NOT NULL,         -- Gmail 邮件 ID
    thread_id         TEXT,
    internal_ms       INTEGER,               -- Gmail 内部接收时间（毫秒时间戳）
    received_at       TEXT,                  -- 接收时间（本地时间，ISO 格式）
    sender            TEXT,
    to_addr           TEXT,
    cc_addr           TEXT,
    subject           TEXT,
    labels            TEXT,                  -- JSON 文本（列表）
    attachments       TEXT,                  -- JSON 文本（附件文件名列表）
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
    deadline       TEXT,                     -- YYYY-MM-DD 或 NULL
    summary        TEXT,
    reason         TEXT,
    suspicious     INTEGER CHECK (suspicious IN (0, 1)),
    model          TEXT,
    prompt_version TEXT,
    error          TEXT,                     -- 失败原因；成功为 NULL
    analyzed_at    TEXT
);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_key  TEXT REFERENCES emails(key),
    account    TEXT,
    title      TEXT NOT NULL,
    deadline   TEXT,                         -- YYYY-MM-DD 或 NULL
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

# 联表查询：邮件 + 分析（LEFT JOIN，没有分析的邮件也会返回）
_SELECT_EMAIL = "SELECT e.*, a.* FROM emails AS e LEFT JOIN analyses AS a ON a.email_key = e.key"

# list_emails 的排序（固定的 SQL 片段）
_EMAIL_ORDER = {
    # 无优先级（分析失败）的排最前，提醒人工看；再按优先级升序、接收时间倒序
    REVIEW: "a.priority IS NOT NULL, a.priority ASC, e.internal_ms DESC",
    # 最近丢弃的排最前
    DISCARDED: "e.status_changed_at DESC, e.internal_ms DESC",
}
_EMAIL_ORDER_DEFAULT = "e.internal_ms DESC"

# list_todos 的排序（固定的 SQL 片段）；没有截止日期/优先级的放最后
_TODO_ORDER = {
    SORT_DEADLINE: "deadline IS NULL, deadline ASC, priority IS NULL, priority ASC, id ASC",
    SORT_PRIORITY: "priority IS NULL, priority ASC, deadline IS NULL, deadline ASC, id ASC",
}


# ---------------------------------------------------------------- 内部工具

@contextmanager
def _connect():
    """打开一个新连接，离开 with 时关闭。

    用法：with _connect() as conn, conn:
        第一个 with 负责关闭连接；第二个 "with conn" 负责事务（正常结束自动提交，出错自动回滚）。
        只读查询可以只写 with _connect() as conn:
    """
    conn = sqlite3.connect(DB_PATH)   # 运行时读取 DB_PATH，测试改路径后立即生效
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now():
    """当前本地时间，ISO 格式，精确到秒。"""
    return datetime.now().isoformat(timespec="seconds")


def _check_status(status):
    """状态必须是四个常量之一。"""
    if status not in ALL_STATUSES:
        raise ValueError(f"未知的邮件状态：{status!r}")


def _check_date(value):
    """日期必须是 YYYY-MM-DD 或空；空字符串统一转成 None。返回规范后的值。"""
    if value in (None, ""):
        return None
    datetime.strptime(value, "%Y-%m-%d")   # 格式不对会抛 ValueError
    return value


def _to_flag(value):
    """把 True/False 转成 1/0；None 保持 None（分析失败时没有这个值）。"""
    return None if value is None else int(bool(value))


def _to_int(value):
    """转成整数；None 保持 None。"""
    return None if value is None else int(value)


def _unique(items):
    """去重并保持原顺序，返回列表。"""
    return list(dict.fromkeys(items))


def _row_to_email(row):
    """把联表查询的一行转成普通 dict；分析结果放在 "analysis" 键下，没有分析则为 None。"""
    email = {col: row[col] for col in EMAIL_COLUMNS}
    email["labels"] = json.loads(email["labels"] or "[]")
    email["attachments"] = json.loads(email["attachments"] or "[]")
    has_analysis = row["email_key"] is not None
    email["analysis"] = {col: row[col] for col in ANALYSIS_COLUMNS} if has_analysis else None
    return email


def _todo_title(row):
    """待办标题：默认用分析里的 action；action 为空时用邮件主题。"""
    action = (row["action"] or "").strip()
    return action or (row["subject"] or "")


# ---------------------------------------------------------------- 初始化

def init_db():
    """建表、建索引。重复运行不会出错，也不会清空已有数据。"""
    with _connect() as conn, conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- 邮件

def email_exists(key):
    """这封邮件是否已经在库里。"""
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM emails WHERE key = ?", (key,)).fetchone()
    return row is not None


def insert_email(record):
    """保存一封邮件（record 为 fetch_mail.fetch_one() 返回的字典）。

    已存在就忽略；新邮件状态为 new。返回是否新增。
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
    """该账号库里最新一封邮件的 internal_ms；没有邮件时返回 None。"""
    with _connect() as conn:
        row = conn.execute("SELECT MAX(internal_ms) AS m FROM emails WHERE account = ?",
                           (account,)).fetchone()
    return row["m"]


def list_emails(status, account=None, priority=None, limit=None):
    """列出某个状态的邮件，返回 dict 列表，每封带 "analysis"（没有分析时为 None）。

    排序：
        review     无优先级（分析失败）的排最前，再按优先级升序、接收时间倒序
        discarded  按丢弃时间（status_changed_at）倒序
        其他       按接收时间倒序
    account / priority / limit 为 None 表示不筛选、不限制数量。
    """
    _check_status(status)
    # 只拼接上面定义好的固定 SQL 片段；所有变量都放进 params，用 ? 传入
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
    """返回一封邮件及其分析（"analysis" 键）；邮件不存在时返回 None。"""
    with _connect() as conn:
        row = conn.execute(_SELECT_EMAIL + " WHERE e.key = ?", (key,)).fetchone()
    return _row_to_email(row) if row else None


def set_status(keys, status):
    """批量修改邮件状态并更新 status_changed_at。返回实际改了几封（本来就是该状态的不算）。"""
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
    """把邮件改回 new 并删除对应的分析（改完提示词后重跑评估用）。返回改了几封。

    注意：只动邮件和分析，已经建好的待办不会删除。
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


# ---------------------------------------------------------------- 分析

def save_analysis(key, result, model, prompt_version, error=None):
    """写入或覆盖一封邮件的分析，并把邮件状态改为 review（同一事务）。

    result 是分析结果字典，键为 category、priority、needs_action、action、deadline、
    summary、reason、suspicious；分析失败时可传 None，并在 error 里写失败原因。
    邮件不存在时抛 KeyError；priority 不在 1–4 时抛 sqlite3.IntegrityError；
    deadline 不是 YYYY-MM-DD 时抛 ValueError。出错时整个操作回滚。
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
            raise KeyError(f"邮件不存在：{key}")
        conn.execute(
            "INSERT OR REPLACE INTO analyses (email_key, category, priority, needs_action, "
            "action, deadline, summary, reason, suspicious, model, prompt_version, error, "
            "analyzed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


# ---------------------------------------------------------------- 待办

def add_todos(keys):
    """对每封邮件用分析结果新建待办，并把邮件状态改为 todo（同一事务）。返回新建了几条。

    标题默认用 action，action 为空时用邮件主题；截止日期、优先级从分析复制。
    已经是 todo 的邮件、不存在的邮件会跳过，不重复建。
    """
    keys = _unique(keys)
    now = _now()
    created = 0
    with _connect() as conn, conn:
        for key in keys:
            # 先改状态：已经是 todo 或邮件不存在时改不动（rowcount 为 0），直接跳过
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
    """列出待办，返回 dict 列表。

    done：False 看未完成，True 看已完成。
    sort："deadline" 按截止日期升序（无截止日期的放最后），"priority" 按优先级升序（无优先级的放最后）。
    """
    if sort not in _TODO_ORDER:
        raise ValueError(f"未知的排序方式：{sort!r}")
    # 只拼接固定的 SQL 片段；变量都用 ? 传入
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
    """返回一条待办；不存在时返回 None。"""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return dict(row) if row else None


def update_todo(todo_id, title, deadline, note):
    """修改待办的标题、截止日期（YYYY-MM-DD 或空）、备注。返回是否改到了。"""
    deadline = _check_date(deadline)
    with _connect() as conn, conn:
        cur = conn.execute("UPDATE todos SET title = ?, deadline = ?, note = ? WHERE id = ?",
                           (title, deadline, note, todo_id))
        return cur.rowcount == 1


def mark_done(ids):
    """批量标记为已完成并记录完成时间。返回改了几条（本来已完成的不算）。"""
    ids = _unique(ids)
    if not ids:
        return 0
    now = _now()
    with _connect() as conn, conn:
        cur = conn.executemany("UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND done = 0",
                               [(now, i) for i in ids])
        return cur.rowcount


def undo_done(ids):
    """批量撤销完成（改回未完成，清空完成时间）。返回改了几条。"""
    ids = _unique(ids)
    if not ids:
        return 0
    with _connect() as conn, conn:
        cur = conn.executemany("UPDATE todos SET done = 0, done_at = NULL WHERE id = ? AND done = 1",
                               [(i,) for i in ids])
        return cur.rowcount


def delete_todos(ids):
    """批量删除待办，并把对应邮件从 todo 改为 discarded（同一事务）。返回删了几条。

    邮件进入"已丢弃"后可以恢复为 review，再次加入待办会新建一条。
    待办没有来源邮件、或邮件已不是 todo 状态时，只删待办，不改邮件。
    """
    ids = _unique(ids)
    if not ids:
        return 0
    now = _now()
    deleted = 0
    with _connect() as conn, conn:
        for todo_id in ids:
            row = conn.execute("SELECT email_key FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if row is None:                      # 待办已不存在，跳过
                continue
            conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
            deleted += 1
            if row["email_key"] is not None:
                conn.execute(
                    "UPDATE emails SET status = ?, status_changed_at = ? WHERE key = ? AND status = ?",
                    (DISCARDED, now, row["email_key"], TODO),
                )
    return deleted


# ---------------------------------------------------------------- 统计

def count_by_status():
    """每个账号每个状态的邮件数量，例如 {"work": {"new": 3, "review": 5, "todo": 1, "discarded": 0}}。"""
    with _connect() as conn:
        rows = conn.execute("SELECT account, status, COUNT(*) AS n FROM emails "
                            "GROUP BY account, status ORDER BY account").fetchall()
    counts = {}
    for r in rows:
        counts.setdefault(r["account"], {s: 0 for s in ALL_STATUSES})[r["status"]] = r["n"]
    return counts
