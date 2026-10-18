"""拉取邮件：从 accounts.json 里每个启用的账号拉取收件箱的新邮件，清洗后存进数据库 mail.db。

前提：已经用 authorize.py 为每个账号授权（tokens/别名.json 存在）。

用法（在项目文件夹里运行）：
    python fetch_mail.py               库里没有该账号的邮件时拉最近 2 天；之后只拉"库里最新一封之后"的
    python fetch_mail.py --days 1      库里没有该账号的邮件时拉最近 1 天
    python fetch_mail.py --max 50      每个账号最多处理 50 封（默认 200）

其他程序（如网页界面）调用 fetch_all()，它返回汇总字典，不打印任何东西。

只拉收件箱"主要"（Primary）标签页的邮件，社交、推广、更新、论坛都不拉。
这个程序只读邮件，不会修改、移动、删除或标记任何邮件。
"""

import argparse
import base64
import email
import email.policy
import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser

import db

ACCOUNTS_FILE = "accounts.json"
TOKENS_DIR = "tokens"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

OVERLAP_SECONDS = 3600     # 增量拉取时往前多倒退 1 小时，宁可多拉再去重，避免漏邮件
MAX_BODY_CHARS = 4000      # 正文最多保留的字数
MAX_ERROR_CHARS = 200      # 错误信息最多保留的字数（只放进汇总，不含令牌和正文）


# ---------------------------------------------------------------- 文件读取

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_accounts():
    """读取 accounts.json，返回启用的账号列表（没写 enabled 的视为启用）。"""
    data = load_json(ACCOUNTS_FILE, {"accounts": []})
    return [a for a in data.get("accounts", []) if a.get("enabled", True)]


# ---------------------------------------------------------------- 连接 Gmail

def connect(account):
    """读取该账号的令牌，连接 Gmail，并核对邮箱。

    返回 (service, None)；失败时返回 (None, 错误信息)。
    """
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    alias, expected = account["alias"], account["email"]
    path = os.path.join(TOKENS_DIR, f"{alias}.json")
    if not os.path.exists(path):
        return None, f"{alias}：没有令牌，请先运行 python authorize.py {alias}"

    creds = Credentials.from_authorized_user_file(path, SCOPES)
    service = build("gmail", "v1", credentials=creds)
    try:
        # 访问令牌过期时，库会用刷新令牌自动换新
        actual = service.users().getProfile(userId="me").execute()["emailAddress"]
    except RefreshError:
        return None, (f"{alias}：授权已失效（测试状态下约 7 天过期），"
                      f"请运行 python authorize.py {alias} 重新授权")

    if actual.lower() != expected.lower():
        return None, f"{alias}：令牌属于 {actual}，与清单里的 {expected} 不一致，请重新授权"
    return service, None


# ---------------------------------------------------------------- 第一步：拿 ID 清单

def list_message_ids(service, query, limit):
    """列出收件箱里符合条件的邮件 ID（从新到旧），自动翻页，最多 limit 封。"""
    ids, page_token = [], None
    while len(ids) < limit:
        resp = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            q=query,
            maxResults=min(500, limit - len(ids)),
            pageToken=page_token,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


# ---------------------------------------------------------------- 第二步：读内容并清洗

class _HTMLToText(HTMLParser):
    """把 HTML 邮件转成纯文字：丢掉样式和脚本，块级标签换行。"""
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "ul", "ol", "blockquote", "hr"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    parser = _HTMLToText()
    parser.feed(html)
    return "".join(parser.parts)


# 出现这些行，说明下面是引用的历史邮件，截掉
_QUOTE_HEADERS = [
    re.compile(r"^On .+wrote:\s*$"),                        # 英文 Gmail
    re.compile(r".*写道[:：]\s*$"),                          # 中文 Gmail
    re.compile(r"^-{2,}\s*(Original Message|原始邮件)\s*-{2,}", re.I),
]
# 注意：不截断"转发的邮件"（Forwarded message），因为转发内容往往就是这封邮件的重点


def clean_body(text):
    """去掉引用的历史回复和签名档，合并空行，截断过长内容。只是经验规则，不保证完美。"""
    text = text.replace("\r\n", "\n").replace("\xa0", " ").replace("​", "")
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue                                        # 引用行
        if stripped == "--":
            break                                           # 标准签名分隔符 "-- "
        if any(p.match(stripped) for p in _QUOTE_HEADERS) and kept:
            break                                           # 下面都是历史邮件
        kept.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n……（正文过长，已截断）"
    return text


def extract_body(msg):
    """优先取纯文本正文；没有就取 HTML 转文字。Python 的 email 库会自动处理编码和字符集。"""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):              # 字符集声明错误时兜底
        content = part.get_payload(decode=True).decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return clean_body(content)


def fetch_one(service, alias, msg_id):
    """读取一封邮件，返回整理好的字典。"""
    resp = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
    raw_bytes = base64.urlsafe_b64decode(resp["raw"])
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    internal_ms = int(resp.get("internalDate", 0))
    return {
        "key": f"{alias}:{msg_id}",           # 别名 + 邮件 ID 作为唯一标识
        "account": alias,
        "id": msg_id,
        "thread_id": resp.get("threadId"),
        "received_at": datetime.fromtimestamp(internal_ms / 1000).isoformat(timespec="seconds"),
        "internal_ms": internal_ms,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "cc": str(msg.get("Cc", "")),
        "subject": str(msg.get("Subject", "")),
        "labels": resp.get("labelIds", []),
        "snippet": resp.get("snippet", ""),
        "attachments": attachments,
        "body": extract_body(msg),
    }


# ---------------------------------------------------------------- 主流程

def build_query(alias, days):
    """库里有该账号的邮件就只拉最新一封之后的；否则拉最近 days 天。只拉"主要"标签页。"""
    last_ms = db.latest_internal_ms(alias)
    if last_ms:
        # 用秒数时间戳，避免 Gmail 把日期按太平洋时间零点解释
        return f"after:{last_ms // 1000 - OVERLAP_SECONDS} category:primary"
    return f"newer_than:{days}d category:primary"


def list_new_ids(service, alias, days, limit):
    """列出该账号符合条件的邮件 ID，跳过库里已有的。

    返回 (找到的总数, 新邮件 ID 列表)。新邮件按从旧到新排列：
    这样中途中断时，库里"最新一封"不会跑到还没读的邮件前面，下次增量拉取不会漏掉它们。
    """
    ids = list_message_ids(service, build_query(alias, days), limit)
    new_ids = [i for i in ids if not db.email_exists(f"{alias}:{i}")]
    new_ids.reverse()
    return len(ids), new_ids


def save_one(service, alias, msg_id):
    """读取一封邮件并立即存库。返回是否真的新增了一条。"""
    return db.insert_email(fetch_one(service, alias, msg_id))


def describe_error(alias, err):
    """把异常转成一行简短的错误信息（只含异常类型和简短说明）。"""
    return f"{alias}：出错（{type(err).__name__}）：{str(err)[:MAX_ERROR_CHARS]}"


def prepare_account(account, days, max_per_account):
    """连接一个账号并列出新邮件 ID。

    返回 (service, 找到的总数, 新邮件 ID 列表, 问题列表)；连接失败时 service 为 None。
    """
    alias = account["alias"]
    try:
        service, error = connect(account)
        if service is None:
            return None, 0, [], [error]
        found, new_ids = list_new_ids(service, alias, days, max_per_account)
    except Exception as e:                       # 网络、令牌文件损坏等，记下来继续处理其他账号
        return None, 0, [], [describe_error(alias, e)]

    problems = []
    if found >= max_per_account:
        problems.append(f"{alias}：已达到单次上限 {max_per_account} 封，可能还有更早的邮件没拉")
    return service, found, new_ids, problems


def fetch_all(days=2, max_per_account=200, progress=None):
    """拉取所有启用账号的新邮件并存库，返回汇总：

        {"accounts": {别名: {"found": 找到, "new": 新增入库, "failed": 读取失败}},
         "problems": ["work：授权已失效，……", ...]}

    "accounts" 只包含成功连接并列出邮件的账号；连接失败的账号只出现在 "problems" 里。
    progress 是可选回调 progress(fraction, message)，每处理完一封调用一次，
    fraction 在 0–1 之间，覆盖所有账号。不抛异常中断，问题都记在 "problems" 里。
    """
    db.init_db()
    summary = {"accounts": {}, "problems": []}
    try:
        accounts = load_accounts()
    except (OSError, ValueError) as e:           # 文件读不了或 JSON 格式错误
        summary["problems"].append(f"读取 {ACCOUNTS_FILE} 失败（{type(e).__name__}）：{e}")
        return summary
    if not accounts:
        summary["problems"].append(f"{ACCOUNTS_FILE} 里没有启用的账号")
        return summary

    # 先列出所有账号的新邮件 ID，算出总数，才能计算进度
    jobs = []
    for account in accounts:
        service, found, new_ids, problems = prepare_account(account, days, max_per_account)
        summary["problems"].extend(problems)
        if service is not None:
            summary["accounts"][account["alias"]] = {"found": found, "new": 0, "failed": 0}
            jobs.append((account["alias"], service, new_ids))

    # 再逐封读取，每读到一封就立刻存库
    total = sum(len(new_ids) for _, _, new_ids in jobs)
    done = 0
    for alias, service, new_ids in jobs:
        stats = summary["accounts"][alias]
        for msg_id in new_ids:
            try:
                if save_one(service, alias, msg_id):
                    stats["new"] += 1
            except Exception:                    # 单封失败不影响其他邮件
                stats["failed"] += 1
            done += 1
            if progress is not None:
                progress(done / total, f"{alias}：已处理 {done}/{total} 封")
    return summary


# ---------------------------------------------------------------- 命令行入口

def format_summary(summary):
    """把汇总字典排成几行文字，供命令行打印。"""
    lines = [f"[{alias}] 找到 {s['found']} 封，新增 {s['new']} 封，失败 {s['failed']} 封"
             for alias, s in summary["accounts"].items()]
    if summary["problems"]:
        lines.append("问题：")
        lines.extend(f"  - {p}" for p in summary["problems"])
    return "\n".join(lines) if lines else "没有处理任何账号。"


def main():
    ap = argparse.ArgumentParser(description="从多个 Gmail 账号拉取收件箱新邮件，存进 mail.db")
    ap.add_argument("--days", type=int, default=2,
                    help="库里没有该账号的邮件时，拉最近几天（默认 2）")
    ap.add_argument("--max", type=int, default=200, help="每个账号单次最多处理几封（默认 200）")
    args = ap.parse_args()
    print(format_summary(fetch_all(days=args.days, max_per_account=args.max)))


if __name__ == "__main__":
    main()
