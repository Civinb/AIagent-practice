"""网页界面：用 Gradio Blocks 在本机浏览器里分拣邮件。

用法（在项目文件夹里运行，fetch_mail.py 用相对路径读 accounts.json 和 tokens/）：
    python app.py
然后打开终端里显示的地址（只监听 127.0.0.1，不对外公开）。

三个标签页：
    分拣    拉取并分析、筛选、勾选后加入待办或丢弃、查看单封详情
    待办    查看未完成待办（带紧急标记）、标记完成、删除、编辑标题/截止日期/备注、撤销完成
    已丢弃  查看最近丢弃的邮件，勾选后恢复到待审
界面只读写本地数据库 mail.db，不发送、不删除、不修改 Gmail 里的任何邮件。
"""

import re
from datetime import date, datetime
from email.utils import parseaddr

import gradio as gr

import analyze
import db
import fetch_mail

# ---------------------------------------------------------------- 常量

ALL = "全部"                                       # 筛选下拉框里"不筛选"的选项
PRIORITY_CHOICES = [ALL, "P1", "P2", "P3", "P4"]
DEFAULT_CHECKED_PRIORITIES = (1, 2)                # 默认勾选：P1/P2 且需要行动
SUBJECT_MAX_CHARS = 30                             # 列表里主题最多显示的字数
SENDER_MAX_CHARS = 20                              # 列表里发件人名最多显示的字数
MAX_ERROR_CHARS = 200                              # 状态文字里错误信息最多显示的字数

FETCH_SHARE = 0.5                                  # 进度条：前一半给拉取，后一半给分析

# 待办页
SORT_CHOICES = [("按截止日期", db.SORT_DEADLINE), ("按优先级", db.SORT_PRIORITY)]
DONE_LIMIT = 30                                    # "已完成"里最多显示最近完成的条数
URGENT_DAYS = 3                                    # 剩 1–3 天的显示"🟡 剩 N 天"
TITLE_MAX_CHARS = 40                               # 待办列表里标题最多显示的字数

# 已丢弃页
DISCARDED_LIMIT = 100                              # 最多显示最近丢弃的封数

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")   # Markdown 里需要转义的字符
_DATE_FORMAT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")  # 截止日期必须严格是 YYYY-MM-DD


# ---------------------------------------------------------------- 文字工具

def md_escape(text):
    """转义 Markdown 特殊字符，防止邮件内容（不可信）被渲染成链接、图片等。"""
    return _MD_SPECIAL.sub(r"\\\1", "" if text is None else str(text))


def code_block(text):
    """把正文原样放进代码块；围栏比正文里最长的连续反引号还长，正文无法提前结束代码块。"""
    text = text or ""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def truncate(text, limit):
    """超过 limit 字就截断并加省略号。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def sender_name(sender):
    """发件人显示名：有名字用名字，没有就用邮箱地址。"""
    name, address = parseaddr(sender or "")
    return name or address or "（未知发件人）"


def short_date(deadline):
    """YYYY-MM-DD → 10/17；格式不对就原样返回。"""
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d")
    except (TypeError, ValueError):
        return deadline
    return f"{d.month}/{d.day}"


def describe_error(err):
    """简短的错误说明：类型 + 前 MAX_ERROR_CHARS 个字。"""
    return f"{type(err).__name__}：{str(err)[:MAX_ERROR_CHARS]}"


# ---------------------------------------------------------------- 筛选

def account_filter(value):
    """账号下拉框的值 → db.list_emails 的 account 参数（"全部"为 None）。"""
    return None if value in (None, ALL) else value


def priority_filter(value):
    """优先级下拉框的值 → db.list_emails 的 priority 参数（"P1" → 1，"全部"为 None）。"""
    return None if value in (None, ALL) else int(value[1:])


def describe_filter(account, priority):
    """筛选条件的文字说明，如"work 账号"、"全部账号、优先级 P1"。"""
    text = "全部账号" if account_filter(account) is None else f"{account} 账号"
    if priority_filter(priority) is not None:
        text += f"、优先级 {priority}"
    return text


def account_aliases():
    """账号别名列表：accounts.json 里启用的账号在前，库里有邮件但不在清单里的账号补在后面。"""
    try:
        aliases = [a["alias"] for a in fetch_mail.load_accounts()]
    except (OSError, ValueError, KeyError):         # 清单读不了时只用库里的账号
        aliases = []
    for alias in db.count_by_status():
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def list_review(account, priority):
    """当前筛选下的待审邮件。"""
    return db.list_emails(db.REVIEW, account=account_filter(account),
                          priority=priority_filter(priority))


def only_review(keys):
    """只保留现在仍是待审状态的 key（界面可能过期，防止把已处理的邮件再改一次）。"""
    review = {e["key"] for e in db.list_emails(db.REVIEW)}
    return [k for k in keys if k in review]


# ---------------------------------------------------------------- 列表显示

def analysis_failed(email):
    """分析失败（或没有分析记录）的邮件。"""
    a = email["analysis"]
    return a is None or bool(a["error"])


def email_label(email):
    """待审列表里一封邮件的显示文字。"""
    sender = truncate(sender_name(email["sender"]), SENDER_MAX_CHARS)
    subject = truncate(email["subject"], SUBJECT_MAX_CHARS) or "（无主题）"
    if analysis_failed(email):
        return f"⚠ 分析失败 | {email['account']} | {sender} | {subject}"

    a = email["analysis"]
    text = subject
    if a["action"]:
        text += f" → {a['action']}"
    if a["deadline"]:
        text += f"（截止 {short_date(a['deadline'])}）"
    label = f"P{a['priority']} | {email['account']} | {sender} | {text}"
    return "⚠ 疑似钓鱼 " + label if a["suspicious"] else label


def default_checked(email):
    """是否默认勾选：分析成功、优先级为 P1/P2、需要行动。"""
    if analysis_failed(email):
        return False
    a = email["analysis"]
    return a["priority"] in DEFAULT_CHECKED_PRIORITIES and bool(a["needs_action"])


def status_text(message=""):
    """状态文字：各账号待审数量 + 上一次操作的结果。"""
    counts = db.count_by_status()
    per_account = [(alias, counts.get(alias, {}).get(db.REVIEW, 0)) for alias in account_aliases()]
    total = sum(n for _, n in per_account)
    parts = "，".join(f"{md_escape(alias)} {n} 封" for alias, n in per_account) or "无账号"
    text = f"**待审**：{parts}（共 {total} 封）"
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- 详情

def detail_text(key):
    """一封待审邮件的详情 Markdown；没选或已不在待审时给出提示。"""
    if not key:
        return "选择一封邮件查看详情。"
    email = db.get_email(key)
    if email is None or email["status"] != db.REVIEW:
        return "这封邮件已不在待审列表。"

    a = email["analysis"] or {}
    attachments = "、".join(md_escape(n) for n in email["attachments"]) or "无"
    lines = [
        f"### {md_escape(email['subject']) or '（无主题）'}",
        "",
        f"- 账号：{md_escape(email['account'])}",
        f"- 发件人：{md_escape(email['sender'])}",
        f"- 接收时间：{md_escape(email['received_at'])}",
    ]
    if analysis_failed(email):
        lines += [f"- **⚠ 分析失败**：{md_escape(a.get('error') or '没有分析记录')}"]
    else:
        lines += [
            f"- 优先级：P{a['priority']}　类别：{md_escape(a['category'])}"
            f"　需要行动：{'是' if a['needs_action'] else '否'}",
            f"- 该做什么：{md_escape(a['action']) or '无'}",
            f"- 截止日期：{md_escape(a['deadline']) or '无'}",
        ]
        if a["suspicious"]:
            lines += ["- **⚠ 疑似钓鱼**"]
    lines += [f"- 附件：{attachments}"]
    if not analysis_failed(email):
        lines += ["", f"**AI 摘要**：{md_escape(a['summary'])}",
                  "", f"**判断理由**：{md_escape(a['reason'])}"]
    lines += ["", "**正文**", "", code_block(email["body"])]
    return "\n".join(lines)


# ---------------------------------------------------------------- 刷新

def refresh(account, priority, detail_key, message=""):
    """按当前筛选重新生成分拣页，返回值顺序与 SORT_OUTPUTS 一致：

    (待审列表, 状态文字, 详情下拉框, 详情, 确认区, 待确认的 key)
    待审列表的勾选重置为默认勾选；详情下拉框尽量保持原来选中的邮件；确认区一律收起。
    """
    emails = list_review(account, priority)
    choices = [(email_label(e), e["key"]) for e in emails]
    keys = [e["key"] for e in emails]
    checked = [e["key"] for e in emails if default_checked(e)]
    detail_key = detail_key if detail_key in keys else None
    return (
        gr.CheckboxGroup(choices=choices, value=checked),
        status_text(message),
        gr.Dropdown(choices=choices, value=detail_key),
        detail_text(detail_key),
        gr.Row(visible=False),
        None,
    )


def error_outputs(err):
    """出错时：只在状态文字里显示错误，其他输出保持不变。"""
    return (gr.skip(), f"**出错**：{md_escape(describe_error(err))}",
            gr.skip(), gr.skip(), gr.skip(), gr.skip())


def message_only(message):
    """只更新状态文字（例如没勾选就点了按钮），其他输出保持不变。"""
    return (gr.skip(), status_text(message), gr.skip(), gr.skip(), gr.skip(), gr.skip())


# ---------------------------------------------------------------- 拉取并分析

def scaled_progress(progress, start, share):
    """把 0–1 的子进度映射到总进度条的 [start, start + share] 区间，返回回调 f(fraction, message)。"""
    def callback(fraction, message):
        progress(start + share * fraction, desc=message)
    return callback


def pull_message(fetch_summary, analyze_summary):
    """把拉取汇总和分析汇总排成状态文字。"""
    fetched = "；".join(
        f"{md_escape(alias)} 找到 {s['found']} 封，新增 {s['new']} 封，失败 {s['failed']} 封"
        for alias, s in fetch_summary["accounts"].items()) or "没有成功连接的账号"
    text = (f"**拉取**：{fetched}\n\n"
            f"**分析**：成功 {analyze_summary['analyzed']} 封，失败 {analyze_summary['failed']} 封")
    problems = fetch_summary["problems"] + analyze_summary["errors"]
    if problems:
        text += "\n\n**问题**：\n" + "\n".join(f"- {md_escape(p)}" for p in problems)
    return text


def on_pull(account, priority, detail_key, progress=gr.Progress()):
    """拉取所有账号的新邮件并分析，完成后刷新。"""
    try:
        progress(0, desc="开始拉取……")
        fetch_summary = fetch_mail.fetch_all(progress=scaled_progress(progress, 0, FETCH_SHARE))
        progress(FETCH_SHARE, desc="开始分析……")
        analyze_summary = analyze.analyze_pending(
            progress=scaled_progress(progress, FETCH_SHARE, 1 - FETCH_SHARE))
        return refresh(account, priority, detail_key, pull_message(fetch_summary, analyze_summary))
    except Exception as e:                            # 任何异常都只显示在状态文字里
        return error_outputs(e)


# ---------------------------------------------------------------- 其他事件

def on_refresh(account, priority, detail_key):
    """打开页面、改筛选时刷新。"""
    try:
        return refresh(account, priority, detail_key)
    except Exception as e:
        return error_outputs(e)


def on_add_todos(selected, account, priority, detail_key):
    """把勾选的邮件加入待办。"""
    try:
        if not selected:
            return message_only("没有勾选任何邮件，未做任何操作。")
        created = db.add_todos(only_review(selected))
        return refresh(account, priority, detail_key, f"已加入待办 {created} 封。")
    except Exception as e:
        return error_outputs(e)


def on_discard_selected(selected, account, priority, detail_key):
    """丢弃勾选的邮件。"""
    try:
        if not selected:
            return message_only("没有勾选任何邮件，未做任何操作。")
        changed = db.set_status(only_review(selected), db.DISCARDED)
        return refresh(account, priority, detail_key, f"已丢弃 {changed} 封。")
    except Exception as e:
        return error_outputs(e)


def on_ask_discard_rest(selected, account, priority):
    """"其余全部丢弃"第一步：算出当前筛选下未勾选的 key，显示确认区。

    返回 (确认区, 确认说明, 待确认的 key, 状态文字)。
    """
    try:
        chosen = set(selected or [])
        rest = [e["key"] for e in list_review(account, priority) if e["key"] not in chosen]
        if not rest:
            return (gr.Row(visible=False), gr.skip(), None,
                    status_text(f"{describe_filter(account, priority)}下没有未勾选的邮件，未做任何操作。"))
        question = f"将丢弃（{md_escape(describe_filter(account, priority))}）未勾选的 {len(rest)} 封，确定吗？"
        return gr.Row(visible=True), question, rest, gr.skip()
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**出错**：{md_escape(describe_error(e))}"


def on_confirm_discard_rest(pending, account, priority, detail_key):
    """"其余全部丢弃"第二步：丢弃第一步记下的 key，然后收起确认区、清空记录并刷新。"""
    try:
        changed = db.set_status(only_review(pending or []), db.DISCARDED)
        return refresh(account, priority, detail_key, f"已丢弃 {changed} 封。")
    except Exception as e:
        return error_outputs(e)


def on_cancel_discard_rest():
    """取消：只收起确认区、清空记录。返回 (确认区, 待确认的 key)。"""
    return gr.Row(visible=False), None


def on_select_detail(key):
    """详情下拉框选中一封邮件时显示详情。"""
    try:
        return detail_text(key)
    except Exception as e:
        return f"**出错**：{md_escape(describe_error(e))}"


# ================================================================ 待办页

# ---------------------------------------------------------------- 日期

def parse_deadline(text):
    """校验编辑框里的截止日期：空 → None；严格的 YYYY-MM-DD 且是真实日期 → 原样返回。

    不合法时抛 ValueError（例如"下周五"、"2026-2-3"、"2026-02-30"）。
    """
    text = (text or "").strip()
    if not text:
        return None
    if not _DATE_FORMAT.match(text):
        raise ValueError("格式不是 YYYY-MM-DD")
    datetime.strptime(text, "%Y-%m-%d")             # 2026-02-30 这类不存在的日期会抛 ValueError
    return text


def days_left(deadline, today):
    """距离截止日期还有几天（今天到期为 0，已过期为负数）；无截止日期或格式不对时返回 None。"""
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d - today).days


def urgency_mark(deadline, today):
    """紧急标记：已过期 / 今天到期 / 剩 1–3 天；其余或无截止日期返回空字符串。"""
    n = days_left(deadline, today)
    if n is None or n > URGENT_DAYS:
        return ""
    if n < 0:
        return "🔴 已过期"
    if n == 0:
        return "🟡 今天到期"
    return f"🟡 剩 {n} 天"


# ---------------------------------------------------------------- 待办列表显示

def todo_email(todo, cache):
    """待办对应的来源邮件（带分析）；没有来源或邮件已不在库里时返回 None。cache 避免重复查询。"""
    key = todo["email_key"]
    if not key:
        return None
    if key not in cache:
        cache[key] = db.get_email(key)
    return cache[key]


def todo_parts(todo, email):
    """待办显示文字的公共部分：[标题, 截止, 账号 · 发件人]。"""
    title = truncate(todo["title"], TITLE_MAX_CHARS) or "（无标题）"
    deadline = f"截止 {short_date(todo['deadline'])}" if todo["deadline"] else "无截止日期"
    sender = truncate(sender_name(email["sender"] if email else None), SENDER_MAX_CHARS)
    return [title, deadline, f"{todo['account']} · {sender}"]


def todo_label(todo, email, today):
    """未完成待办的显示文字，如"🔴 已过期 | 标题 | 截止 10/17 | work · 发件人"。"""
    mark = urgency_mark(todo["deadline"], today)
    return " | ".join(([mark] if mark else []) + todo_parts(todo, email))


def done_label(todo, email):
    """已完成待办的显示文字，如"标题 | 截止 10/17 | work · 发件人 | 完成于 10/18"。"""
    done_day = short_date((todo["done_at"] or "")[:10])
    return " | ".join(todo_parts(todo, email) + [f"完成于 {done_day}"])


def list_open(account, sort):
    """当前账号筛选下的未完成待办，按所选方式排序。"""
    return db.list_todos(done=False, account=account_filter(account), sort=sort)


def list_recent_done(account):
    """当前账号筛选下最近完成的 DONE_LIMIT 条（按完成时间倒序）。"""
    done = db.list_todos(done=True, account=account_filter(account))
    done.sort(key=lambda t: t["done_at"] or "", reverse=True)
    return done[:DONE_LIMIT]


def only_open(ids):
    """只保留现在仍未完成的待办 id（界面可能过期，防止误删已完成的）。"""
    open_ids = {t["id"] for t in db.list_todos(done=False)}
    return [i for i in ids if i in open_ids]


def todo_status_text(account, message=""):
    """待办页状态文字：当前筛选下未完成的数量、已过期 / 3 天内到期的数量 + 上一次操作的结果。"""
    where = "全部账号" if account_filter(account) is None else f"{account} 账号"
    today = date.today()
    left = [days_left(t["deadline"], today) for t in list_open(account, db.SORT_DEADLINE)]
    overdue = sum(1 for n in left if n is not None and n < 0)
    soon = sum(1 for n in left if n is not None and 0 <= n <= URGENT_DAYS)
    text = (f"**未完成**：{md_escape(where)} {len(left)} 条\n\n"
            f"🔴 已过期 {overdue} 条　🟡 {URGENT_DAYS} 天内到期 {soon} 条")
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- 编辑区

def source_text(todo):
    """编辑区下方的来源邮件说明：发件人、主题、AI 摘要。"""
    email = todo_email(todo, {})
    if email is None:
        return "找不到来源邮件。"
    a = email["analysis"]
    summary = md_escape(a["summary"]) if a and not a["error"] and a["summary"] else "无"
    return "\n".join([
        "**来源邮件**",
        "",
        f"- 发件人：{md_escape(email['sender']) or '（未知发件人）'}",
        f"- 主题：{md_escape(email['subject']) or '（无主题）'}",
        f"- AI 摘要：{summary}",
    ])


def editor_values(todo_id):
    """编辑区四个输出：(标题, 截止日期, 备注, 来源说明)。没选或待办已不存在时清空。"""
    todo = db.get_todo(todo_id) if todo_id is not None else None
    if todo is None or todo["done"]:
        return "", "", "", "选择一条待办进行编辑。"
    return todo["title"], todo["deadline"] or "", todo["note"] or "", source_text(todo)


# ---------------------------------------------------------------- 刷新

def refresh_todo(account, sort, edit_id, message="", reload_editor=False):
    """重新生成待办页，返回值顺序与 build_todo_tab 里的 outputs 一致：

    (未完成列表, 编辑下拉框, 标题, 截止日期, 备注, 来源说明, 已完成列表, 确认区, 待确认的 id, 状态文字)
    两个列表的勾选清空；确认区收起。编辑下拉框尽量保持原来选中的待办：
    选中的还在时编辑框保持不动（不覆盖没保存的修改），reload_editor=True 时从数据库重新填入；
    选中的已不在未完成列表时清空编辑区。
    """
    today = date.today()                            # 紧急标记按本地日期计算
    cache = {}
    open_todos = list_open(account, sort)
    choices = [(todo_label(t, todo_email(t, cache), today), t["id"]) for t in open_todos]
    done_choices = [(done_label(t, todo_email(t, cache)), t["id"])
                    for t in list_recent_done(account)]

    open_ids = [t["id"] for t in open_todos]
    edit_id = edit_id if edit_id in open_ids else None
    if edit_id is None or reload_editor:
        editor = editor_values(edit_id)
    else:
        editor = (gr.skip(),) * 4
    return (
        gr.CheckboxGroup(choices=choices, value=[]),
        gr.Dropdown(choices=choices, value=edit_id),
        *editor,
        gr.CheckboxGroup(choices=done_choices, value=[]),
        gr.Row(visible=False),
        None,
        todo_status_text(account, message),
    )


def todo_error_outputs(err):
    """出错时：只在状态文字里显示错误，其他输出保持不变。"""
    return (gr.skip(),) * 9 + (f"**出错**：{md_escape(describe_error(err))}",)


def todo_message_only(account, message):
    """只更新状态文字（例如没勾选就点了按钮），其他输出保持不变。"""
    return (gr.skip(),) * 9 + (todo_status_text(account, message),)


# ---------------------------------------------------------------- 待办页事件

def on_todo_refresh(account, sort, edit_id):
    """打开页面、改筛选/排序、分拣页加入待办之后刷新。"""
    try:
        return refresh_todo(account, sort, edit_id)
    except Exception as e:
        return todo_error_outputs(e)


def on_mark_done(selected, account, sort, edit_id):
    """把勾选的待办标记为已完成。"""
    try:
        if not selected:
            return todo_message_only(account, "没有勾选任何待办，未做任何操作。")
        changed = db.mark_done(selected)
        return refresh_todo(account, sort, edit_id, f"已标记完成 {changed} 条。")
    except Exception as e:
        return todo_error_outputs(e)


def on_ask_delete(selected, account):
    """"删除"第一步：记下勾选的 id，显示确认区。返回 (确认区, 确认说明, 待确认的 id, 状态文字)。"""
    try:
        if not selected:
            return (gr.Row(visible=False), gr.skip(), None,
                    todo_status_text(account, "没有勾选任何待办，未做任何操作。"))
        question = f"将删除勾选的 {len(selected)} 条待办（删除后不能恢复），确定吗？"
        return gr.Row(visible=True), question, list(selected), gr.skip()
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**出错**：{md_escape(describe_error(e))}"


def on_confirm_delete(pending, account, sort, edit_id):
    """"删除"第二步：删除第一步记下的 id（只删仍未完成的），然后收起确认区并刷新。"""
    try:
        deleted = db.delete_todos(only_open(pending or []))
        return refresh_todo(account, sort, edit_id, f"已删除 {deleted} 条待办。")
    except Exception as e:
        return todo_error_outputs(e)


def on_cancel_delete():
    """取消删除：只收起确认区、清空记录。返回 (确认区, 待确认的 id)。"""
    return gr.Row(visible=False), None


def on_select_todo(todo_id):
    """编辑下拉框选中一条待办时，把标题、截止日期、备注、来源说明填入编辑区。"""
    try:
        return editor_values(todo_id)
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**出错**：{md_escape(describe_error(e))}"


def on_save_todo(edit_id, title, deadline, note, account, sort):
    """保存编辑：截止日期必须为空或合法日期，不合法只提示、不保存。只改待办表。"""
    try:
        if edit_id is None:
            return todo_message_only(account, "请先在下拉框里选择一条待办。")
        title = (title or "").strip()
        if not title:
            return todo_message_only(account, "标题不能为空，未保存。")
        try:
            deadline = parse_deadline(deadline)
        except ValueError:
            return todo_message_only(
                account, f"截止日期“{md_escape(deadline)}”不合法，请填 YYYY-MM-DD 或留空。未保存。")
        if not db.update_todo(edit_id, title, deadline, (note or "").strip()):
            return refresh_todo(account, sort, None, "这条待办已不存在，未保存。")
        return refresh_todo(account, sort, edit_id, "已保存。", reload_editor=True)
    except Exception as e:
        return todo_error_outputs(e)


def on_undo_done(selected, account, sort, edit_id):
    """把"已完成"里勾选的待办改回未完成。"""
    try:
        if not selected:
            return todo_message_only(account, "没有勾选任何已完成的待办，未做任何操作。")
        changed = db.undo_done(selected)
        return refresh_todo(account, sort, edit_id, f"已撤销完成 {changed} 条。")
    except Exception as e:
        return todo_error_outputs(e)


# ================================================================ 已丢弃页

# ---------------------------------------------------------------- 列表显示

def discard_time(email):
    """丢弃时间的显示文字：status_changed_at（ISO 格式）→ 10/18 19:25；格式不对就原样返回。"""
    value = email["status_changed_at"]
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "（时间未知）"
    return f"{t.month}/{t.day} {t:%H:%M}"


def discarded_label(email):
    """已丢弃列表里一封邮件的显示文字：丢弃时间 + 与分拣页相同的文字。"""
    return f"丢弃于 {discard_time(email)} | {email_label(email)}"


def only_discarded(keys):
    """只保留现在仍是已丢弃状态的 key（界面可能过期，防止把已处理的邮件再改一次）。"""
    discarded = {e["key"] for e in db.list_emails(db.DISCARDED)}
    return [k for k in keys if k in discarded]


def discarded_status_text(message=""):
    """已丢弃页状态文字：已丢弃总数、列表显示的数量 + 上一次操作的结果。"""
    total = sum(c[db.DISCARDED] for c in db.count_by_status().values())
    text = f"**已丢弃**：共 {total} 封，下面列出最近丢弃的 {min(total, DISCARDED_LIMIT)} 封"
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- 刷新

def refresh_discarded(message=""):
    """重新生成已丢弃页，返回值顺序与 build_discarded_tab 里的 outputs 一致：

    (已丢弃列表, 状态文字)
    列表按丢弃时间倒序，最多 DISCARDED_LIMIT 封，勾选清空。
    """
    emails = db.list_emails(db.DISCARDED, limit=DISCARDED_LIMIT)
    choices = [(discarded_label(e), e["key"]) for e in emails]
    return gr.CheckboxGroup(choices=choices, value=[]), discarded_status_text(message)


def discarded_error_outputs(err):
    """出错时：只在状态文字里显示错误，列表保持不变。"""
    return gr.skip(), f"**出错**：{md_escape(describe_error(err))}"


# ---------------------------------------------------------------- 已丢弃页事件

def on_discarded_refresh():
    """打开页面、分拣页丢弃之后刷新。"""
    try:
        return refresh_discarded()
    except Exception as e:
        return discarded_error_outputs(e)


def on_restore(selected):
    """把勾选的已丢弃邮件恢复到待审。"""
    try:
        if not selected:
            return gr.skip(), discarded_status_text("没有勾选任何邮件，未做任何操作。")
        changed = db.set_status(only_discarded(selected), db.REVIEW)
        return refresh_discarded(f"已恢复到待审 {changed} 封。")
    except Exception as e:
        return discarded_error_outputs(e)


# ---------------------------------------------------------------- 界面

def build_todo_tab(demo):
    """在当前位置搭建"待办"标签页的内容并绑定事件。

    返回刷新本页的事件参数（fn / inputs / outputs），供分拣页"加入待办"之后用 .then 刷新。
    """
    with gr.Row():
        account_dd = gr.Dropdown([ALL] + account_aliases(), value=ALL, label="账号")
        sort_radio = gr.Radio(SORT_CHOICES, value=db.SORT_DEADLINE, label="排序")

    pending_ids = gr.State(None)                        # "删除"等待确认的 id 列表

    with gr.Row():
        # 左栏：未完成待办列表、批量操作、编辑区
        with gr.Column(scale=3):
            open_list = gr.CheckboxGroup(label="未完成待办", show_label=True, show_select_all=True)
            with gr.Row():
                done_btn = gr.Button("标记完成", variant="primary")
                delete_btn = gr.Button("删除", variant="stop")
            with gr.Row(visible=False) as confirm_row:
                confirm_md = gr.Markdown()
                confirm_btn = gr.Button("确认删除", variant="stop", scale=0, min_width=120)
                cancel_btn = gr.Button("取消", scale=0, min_width=120)

            with gr.Group():
                edit_dd = gr.Dropdown(label="编辑待办（当前未完成）", choices=[], value=None)
                with gr.Row():
                    title_tb = gr.Textbox(label="标题", scale=3)
                    deadline_tb = gr.Textbox(label="截止日期", placeholder="YYYY-MM-DD，留空表示无",
                                             scale=2)
                note_tb = gr.Textbox(label="备注", lines=2)
            save_btn = gr.Button("保存")

        # 右栏：提醒（数量、操作结果）、来源邮件、已完成
        with gr.Column(scale=2):
            status_md = gr.Markdown()
            source_md = gr.Markdown("选择一条待办进行编辑。")
            with gr.Accordion("已完成", open=False):
                done_list = gr.CheckboxGroup(label=f"最近完成的 {DONE_LIMIT} 条", show_label=True)
                undo_btn = gr.Button("撤销完成")

    # 刷新类事件的输出，顺序与 refresh_todo() 的返回值一致
    outputs = [open_list, edit_dd, title_tb, deadline_tb, note_tb, source_md,
               done_list, confirm_row, pending_ids, status_md]
    view = [account_dd, sort_radio, edit_dd]          # 刷新需要的输入：筛选、排序、当前编辑的待办

    demo.load(on_todo_refresh, inputs=view, outputs=outputs)
    account_dd.change(on_todo_refresh, inputs=view, outputs=outputs)
    sort_radio.change(on_todo_refresh, inputs=view, outputs=outputs)

    done_btn.click(on_mark_done, inputs=[open_list] + view, outputs=outputs)
    delete_btn.click(on_ask_delete, inputs=[open_list, account_dd],
                     outputs=[confirm_row, confirm_md, pending_ids, status_md])
    confirm_btn.click(on_confirm_delete, inputs=[pending_ids] + view, outputs=outputs)
    cancel_btn.click(on_cancel_delete, inputs=None, outputs=[confirm_row, pending_ids])

    # 只响应用户手动选择；刷新时 refresh_todo() 已经处理了编辑区
    edit_dd.input(on_select_todo, inputs=edit_dd,
                  outputs=[title_tb, deadline_tb, note_tb, source_md])
    save_btn.click(on_save_todo,
                   inputs=[edit_dd, title_tb, deadline_tb, note_tb, account_dd, sort_radio],
                   outputs=outputs)
    undo_btn.click(on_undo_done, inputs=[done_list] + view, outputs=outputs)

    return {"fn": on_todo_refresh, "inputs": view, "outputs": outputs}


def build_discarded_tab(demo):
    """在当前位置搭建"已丢弃"标签页的内容并绑定事件。

    返回 {"restore": "恢复到待审"按钮的事件, "refresh": 刷新本页的事件参数（fn / inputs / outputs）}，
    供 build_app 在恢复之后刷新分拣页、在分拣页丢弃之后刷新本页。
    """
    status_md = gr.Markdown()
    discarded_list = gr.CheckboxGroup(label=f"最近丢弃的 {DISCARDED_LIMIT} 封", show_label=True,
                                      show_select_all=True)
    restore_btn = gr.Button("恢复到待审", variant="primary")

    # 刷新类事件的输出，顺序与 refresh_discarded() 的返回值一致
    outputs = [discarded_list, status_md]

    demo.load(on_discarded_refresh, inputs=None, outputs=outputs)
    restore_event = restore_btn.click(on_restore, inputs=discarded_list, outputs=outputs)

    return {"restore": restore_event,
            "refresh": {"fn": on_discarded_refresh, "inputs": None, "outputs": outputs}}


def build_sort_tab(demo):
    """在当前位置搭建"分拣"标签页的内容并绑定事件。

    返回 {"add": "加入待办"的事件, "discards": ["丢弃选中"、"确认丢弃"的事件],
          "refresh": 刷新本页的事件参数（fn / inputs / outputs）}，
    供 build_app 用 .then 刷新待办页、已丢弃页，以及在已丢弃页恢复之后刷新本页。
    """
    with gr.Row():
        pull_btn = gr.Button("拉取并分析", variant="primary", scale=0, min_width=160)
        status_md = gr.Markdown()
    with gr.Row():
        account_dd = gr.Dropdown([ALL] + account_aliases(), value=ALL, label="账号")
        priority_dd = gr.Dropdown(PRIORITY_CHOICES, value=ALL, label="优先级")

    mail_list = gr.CheckboxGroup(label="待审邮件", show_label=True, show_select_all=True)
    with gr.Row():
        add_btn = gr.Button("加入待办", variant="primary")
        discard_btn = gr.Button("丢弃选中")
        discard_rest_btn = gr.Button("其余全部丢弃", variant="stop")

    with gr.Row(visible=False) as confirm_row:
        confirm_md = gr.Markdown()
        confirm_btn = gr.Button("确认丢弃", variant="stop", scale=0, min_width=120)
        cancel_btn = gr.Button("取消", scale=0, min_width=120)
    pending_keys = gr.State(None)                       # "其余全部丢弃"等待确认的 key 列表

    detail_dd = gr.Dropdown(label="查看详情（当前待审邮件）", choices=[], value=None)
    detail_md = gr.Markdown("选择一封邮件查看详情。")

    # 刷新类事件的输出，顺序与 refresh() 的返回值一致
    outputs = [mail_list, status_md, detail_dd, detail_md, confirm_row, pending_keys]
    filters = [account_dd, priority_dd]

    demo.load(on_refresh, inputs=filters + [detail_dd], outputs=outputs)
    account_dd.change(on_refresh, inputs=filters + [detail_dd], outputs=outputs)
    priority_dd.change(on_refresh, inputs=filters + [detail_dd], outputs=outputs)

    pull_btn.click(on_pull, inputs=filters + [detail_dd], outputs=outputs)
    add_event = add_btn.click(on_add_todos, inputs=[mail_list] + filters + [detail_dd],
                              outputs=outputs)
    discard_event = discard_btn.click(on_discard_selected,
                                      inputs=[mail_list] + filters + [detail_dd], outputs=outputs)

    discard_rest_btn.click(on_ask_discard_rest, inputs=[mail_list] + filters,
                           outputs=[confirm_row, confirm_md, pending_keys, status_md])
    confirm_event = confirm_btn.click(on_confirm_discard_rest,
                                      inputs=[pending_keys] + filters + [detail_dd],
                                      outputs=outputs)
    cancel_btn.click(on_cancel_discard_rest, inputs=None, outputs=[confirm_row, pending_keys])

    # 只响应用户手动选择；刷新时 refresh() 已经直接更新了详情
    detail_dd.input(on_select_detail, inputs=detail_dd, outputs=detail_md)

    return {"add": add_event,
            "discards": [discard_event, confirm_event],
            "refresh": {"fn": on_refresh, "inputs": filters + [detail_dd], "outputs": outputs}}


def build_app():
    """搭建整个界面：分拣、待办、已丢弃三个标签页。"""
    with gr.Blocks(title="邮件分拣") as demo:
        with gr.Tabs():
            with gr.Tab("分拣"):
                sort_tab = build_sort_tab(demo)
            with gr.Tab("待办"):
                refresh_todo_tab = build_todo_tab(demo)
            with gr.Tab("已丢弃"):
                discarded_tab = build_discarded_tab(demo)
        # 以下刷新都在前一个事件完成后（无论成败）进行，切到对应标签页立即可见
        sort_tab["add"].then(**refresh_todo_tab)               # 加入待办 → 刷新待办页
        for event in sort_tab["discards"]:                     # 丢弃选中 / 确认丢弃 → 刷新已丢弃页
            event.then(**discarded_tab["refresh"])
        discarded_tab["restore"].then(**sort_tab["refresh"])   # 恢复到待审 → 刷新分拣页
    return demo


def main():
    db.init_db()
    # 只监听本机；share=False 显式写出，防止环境变量 GRADIO_SHARE=True 时生成公网链接
    build_app().launch(server_name="127.0.0.1", share=False)


if __name__ == "__main__":
    main()
