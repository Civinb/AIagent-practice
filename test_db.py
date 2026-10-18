"""db.py 的测试：用临时数据库文件跑通完整流程，全部用 assert 检查，跑完删除临时文件。

用法（在项目文件夹里运行）：
    python test_db.py
不会碰正式的 mail.db。
"""

import os
import shutil
import sqlite3
import tempfile

import db


def make_record(alias, msg_id, internal_ms, subject):
    """造一封假邮件，字段与 fetch_mail.fetch_one() 返回的一致。"""
    return {
        "key": f"{alias}:{msg_id}",
        "account": alias,
        "id": msg_id,
        "thread_id": "t-" + msg_id,
        "received_at": "2026-10-14T09:00:00",
        "internal_ms": internal_ms,
        "from": "测试 <test@example.com>",
        "to": "me@example.com",
        "cc": "",
        "subject": subject,
        "labels": ["INBOX", "UNREAD"],
        "snippet": "摘要",
        "attachments": ["报价单.pdf"],
        "body": "测试正文",
    }


GOOD_RESULT = {
    "category": "工作", "priority": 2, "needs_action": True, "action": "回复报价",
    "deadline": "2026-10-20", "summary": "对方要报价", "reason": "有明确截止日期",
    "suspicious": False,
}


def run_tests():
    # 1. 初始化两次不出错
    db.init_db()
    db.init_db()

    # 2. 同一记录插入两次只存一条
    a = make_record("work", "m1", 1000, "请报价")
    assert db.insert_email(a) is True
    assert db.insert_email(a) is False
    assert db.email_exists(a["key"])
    assert not db.email_exists("work:不存在")
    news = db.list_emails(db.NEW)
    assert len(news) == 1
    assert news[0]["labels"] == ["INBOX", "UNREAD"] and news[0]["attachments"] == ["报价单.pdf"]
    assert news[0]["sender"] == a["from"] and news[0]["analysis"] is None

    b = make_record("work", "m2", 3000, "看不懂的邮件")
    c = make_record("home", "m3", 2000, "没有行动项的邮件")
    db.insert_email(b)
    db.insert_email(c)
    assert db.latest_internal_ms("work") == 3000
    assert db.latest_internal_ms("没有这个账号") is None

    # 3. 保存分析后状态为 review；分析失败的也要返回，且排在最前
    db.save_analysis(a["key"], GOOD_RESULT, "gemini-test", "v1")
    db.save_analysis(b["key"], None, "gemini-test", "v1", error="超时")
    db.save_analysis(c["key"], {**GOOD_RESULT, "action": "", "priority": 4, "deadline": None},
                     "gemini-test", "v1")
    got = db.get_email(a["key"])
    assert got["status"] == db.REVIEW
    assert got["analysis"]["priority"] == 2 and got["analysis"]["needs_action"] == 1
    assert got["analysis"]["error"] is None
    review = db.list_emails(db.REVIEW)
    assert [e["key"] for e in review] == [b["key"], a["key"], c["key"]]   # 失败的(无优先级)最前
    assert review[0]["analysis"]["error"] == "超时"
    assert [e["key"] for e in db.list_emails(db.REVIEW, account="home")] == [c["key"]]
    assert [e["key"] for e in db.list_emails(db.REVIEW, priority=2)] == [a["key"]]
    assert len(db.list_emails(db.REVIEW, limit=1)) == 1

    # 覆盖分析：再存一次只保留一条
    db.save_analysis(a["key"], GOOD_RESULT, "gemini-test", "v1")
    assert db.get_email(a["key"])["analysis"]["model"] == "gemini-test"

    # 不存在的邮件、非法优先级都要报错，且不留下脏数据
    try:
        db.save_analysis("work:不存在", GOOD_RESULT, "m", "v1")
        assert False, "应当抛 KeyError"
    except KeyError:
        pass
    try:
        db.save_analysis(a["key"], {**GOOD_RESULT, "priority": 9}, "其他模型", "v1")
        assert False, "应当抛 IntegrityError"
    except sqlite3.IntegrityError:
        pass
    assert db.get_email(a["key"])["analysis"]["model"] == "gemini-test"   # 已回滚，旧分析还在

    # 4. add_todos 后状态为 todo 且待办存在；重复添加不重复建
    assert db.add_todos([a["key"], c["key"]]) == 2
    assert db.get_email(a["key"])["status"] == db.TODO
    assert db.add_todos([a["key"]]) == 0
    todos = db.list_todos()
    assert len(todos) == 2
    todo_a = next(t for t in todos if t["email_key"] == a["key"])
    todo_c = next(t for t in todos if t["email_key"] == c["key"])
    assert todo_a["title"] == "回复报价"                       # 标题用 action
    assert todo_a["deadline"] == "2026-10-20" and todo_a["priority"] == 2
    assert todo_c["title"] == "没有行动项的邮件"                 # action 为空时用主题
    assert [t["id"] for t in todos] == [todo_a["id"], todo_c["id"]]   # 无截止日期的放最后
    assert [t["id"] for t in db.list_todos(sort="priority")] == [todo_a["id"], todo_c["id"]]
    assert [t["id"] for t in db.list_todos(account="home")] == [todo_c["id"]]

    assert db.update_todo(todo_c["id"], "改过的标题", "2026-10-15", "备注") is True
    edited = db.get_todo(todo_c["id"])
    assert edited["title"] == "改过的标题" and edited["deadline"] == "2026-10-15"
    assert [t["id"] for t in db.list_todos()] == [todo_c["id"], todo_a["id"]]   # 截止日期更早排前

    # 5. mark_done / undo_done
    assert db.mark_done([todo_a["id"]]) == 1
    assert db.mark_done([todo_a["id"]]) == 0                  # 已完成的不再计数
    assert [t["id"] for t in db.list_todos(done=False)] == [todo_c["id"]]
    done_list = db.list_todos(done=True)
    assert [t["id"] for t in done_list] == [todo_a["id"]] and done_list[0]["done_at"]
    assert db.undo_done([todo_a["id"]]) == 1
    restored = db.get_todo(todo_a["id"])
    assert restored["done"] == 0 and restored["done_at"] is None

    assert db.delete_todos([todo_c["id"]]) == 1
    assert db.get_todo(todo_c["id"]) is None

    # 6. 丢弃再恢复
    assert db.set_status([b["key"]], db.DISCARDED) == 1
    assert db.set_status([b["key"]], db.DISCARDED) == 0       # 已是该状态的不计数
    assert [e["key"] for e in db.list_emails(db.DISCARDED)] == [b["key"]]
    assert db.set_status([b["key"]], db.REVIEW) == 1
    assert db.get_email(b["key"])["status"] == db.REVIEW
    assert db.list_emails(db.DISCARDED) == []

    # 统计
    counts = db.count_by_status()
    assert counts["work"] == {db.NEW: 0, db.REVIEW: 1, db.TODO: 1, db.DISCARDED: 0}
    assert counts["home"] == {db.NEW: 0, db.REVIEW: 0, db.TODO: 1, db.DISCARDED: 0}

    # 7. reset_to_new：状态改回 new，分析被删除
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
        print("db.py 全部测试通过")
    finally:
        db.DB_PATH = original_path
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
