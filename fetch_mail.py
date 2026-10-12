"""拉取邮件示例程序：从 accounts.json 里的每个账号拉取收件箱的新邮件，清洗后存到 data/ 文件夹。

前提：已经用 authorize.py 为每个账号授权（tokens/别名.json 存在）。

用法（在项目文件夹里运行）：
    python fetch_mail.py                 首次运行拉最近 2 天；之后只拉"上次之后"的新邮件
    python fetch_mail.py --days 1        首次运行时拉最近 1 天
    python fetch_mail.py --account work  只拉别名为 work 的账号
    python fetch_mail.py --max 50        每个账号最多处理 50 封（默认 200）
    python fetch_mail.py --reset         忽略上次记录，重新按 --days 拉取

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
import sys
from datetime import datetime
from html.parser import HTMLParser

ACCOUNTS_FILE = "accounts.json"
TOKENS_DIR = "tokens"
DATA_DIR = "data"
STATE_FILE = os.path.join("state", "fetch_state.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

OVERLAP_SECONDS = 3600     # 增量拉取时往前多倒退 1 小时，宁可多拉再去重，避免漏邮件
MAX_BODY_CHARS = 4000      # 正文最多保留的字数
SEEN_IDS_KEEP = 3000       # 每个账号记住最近多少个已拉取的邮件 ID，用于去重



# ---------------------------------------------------------------- 文件读写

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_private(path, data):
    """写 JSON 文件；在 Mac/Linux 上设成只有自己能读写（邮件内容属于隐私）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 连接 Gmail

def connect(account):
    """读取该账号的令牌，连接 Gmail，并核对邮箱。失败时返回 None。"""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    alias, expected = account["alias"], account["email"]
    path = os.path.join(TOKENS_DIR, f"{alias}.json")
    if not os.path.exists(path):
        print(f"[跳过] {alias}：没有令牌，请先运行 python authorize.py {alias}")
        return None

    creds = Credentials.from_authorized_user_file(path, SCOPES)
    service = build("gmail", "v1", credentials=creds)
    try:
        # 访问令牌过期时，库会用刷新令牌自动换新
        actual = service.users().getProfile(userId="me").execute()["emailAddress"]
    except RefreshError:
        print(f"[跳过] {alias}：授权已失效（测试状态下约 7 天过期），"
              f"请运行 python authorize.py {alias} 重新授权")
        return None

    if actual.lower() != expected.lower():
        print(f"[跳过] {alias}：令牌属于 {actual}，与清单里的 {expected} 不一致，请重新授权")
        return None
    return service


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

def build_query(acc_state, days, reset):
    """有上次记录就只拉之后的；否则拉最近 days 天。只拉"主要"标签页。"""
    last_ms = acc_state.get("last_internal_ms")
    if last_ms and not reset:
        # 用秒数时间戳，避免 Gmail 把日期按太平洋时间零点解释
        return f"after:{last_ms // 1000 - OVERLAP_SECONDS} category:primary"
    return f"newer_than:{days}d category:primary"


def fetch_account(service, account, acc_state, args):
    alias = account["alias"]
    query = build_query(acc_state, args.days, args.reset)
    seen = set() if args.reset else set(acc_state.get("seen_ids", []))

    ids = list_message_ids(service, query, args.max)
    new_ids = [i for i in ids if i not in seen]
    print(f"[{alias}] 条件 {query}：找到 {len(ids)} 封，其中新邮件 {len(new_ids)} 封")
    if len(ids) >= args.max:
        print(f"[{alias}] 提醒：已达到单次上限 {args.max} 封，可能还有更早的邮件没拉")

    from googleapiclient.errors import HttpError
    results = []
    for n, msg_id in enumerate(new_ids, 1):
        try:
            results.append(fetch_one(service, alias, msg_id))
        except HttpError as e:
            print(f"[{alias}] 读取第 {n} 封（{msg_id}）失败，已跳过：{e}")
    print(f"[{alias}] 成功读取 {len(results)} 封")

    # 更新状态：记住拉过的 ID（按先后顺序，只保留最近的）和最新一封的接收时间
    old_seen = [] if args.reset else acc_state.get("seen_ids", [])
    acc_state["seen_ids"] = (old_seen + [r["id"] for r in results])[-SEEN_IDS_KEEP:]
    if results:
        newest = max(r["internal_ms"] for r in results)
        acc_state["last_internal_ms"] = max(newest, acc_state.get("last_internal_ms") or 0)
    return results


def main():
    ap = argparse.ArgumentParser(description="从多个 Gmail 账号拉取收件箱新邮件")
    ap.add_argument("--days", type=int, default=2, help="首次运行拉最近几天（默认 2）")
    ap.add_argument("--max", type=int, default=200, help="每个账号单次最多处理几封（默认 200）")
    ap.add_argument("--account", help="只处理这个别名的账号")
    ap.add_argument("--reset", action="store_true", help="忽略上次记录，重新按 --days 拉取")
    try:
        fetch_all(ap.parse_args())
    except ValueError as e:
        sys.exit(str(e))


def fetch_all(args):
    """拉取所有（或指定）账号的新邮件并保存。返回 (邮件列表, 保存路径)，没有新邮件时路径为 None。"""
    accounts = [a for a in load_json(ACCOUNTS_FILE, {"accounts": []})["accounts"]
                if a.get("enabled", True)]
    if args.account:
        accounts = [a for a in accounts if a["alias"] == args.account]
    if not accounts:
        raise ValueError("没有要处理的账号，请检查 accounts.json 或 --account 参数。")

    state = load_json(STATE_FILE, {})
    all_mail = []
    for account in accounts:
        service = connect(account)
        if service is None:
            continue
        acc_state = state.setdefault(account["alias"], {})
        all_mail.extend(fetch_account(service, account, acc_state, args))

    save_private(STATE_FILE, state)
    if not all_mail:
        print("\n完成：没有新邮件。")
        return all_mail, None

    all_mail.sort(key=lambda r: r["internal_ms"], reverse=True)   # 所有账号合并，从新到旧
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path, n = os.path.join(DATA_DIR, f"{stamp}.json"), 1
    while os.path.exists(out_path):                                # 避免覆盖已有文件
        n += 1
        out_path = os.path.join(DATA_DIR, f"{stamp}_{n}.json")
    save_private(out_path, {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                            "count": len(all_mail), "messages": all_mail})
    print(f"\n完成：共 {len(all_mail)} 封新邮件，已保存到 {out_path}")
    return all_mail, out_path


if __name__ == "__main__":
    main()
