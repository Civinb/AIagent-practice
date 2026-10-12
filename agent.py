"""
Gemini agent

运行前：
    pip install -U google-genai
    export GEMINI_API_KEY="你的key"      # Windows PowerShell: $env:GEMINI_API_KEY="你的key"
    python agent.py
"""

import argparse
import ast
import json
import operator
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from google import genai

import fetch_mail

MODEL = "gemini-3.5-flash-lite"   # 官方文档示例用的模型；想省额度可换 gemini-3.5-flash-lite
MAX_STEPS = 12                   # 防止模型无限调用工具

client = genai.Client()           # 会从环境变量 GEMINI_API_KEY 读取 key


# ---------------------------------------------------------------------------
# 1. 工具
# ---------------------------------------------------------------------------

def get_current_time(timezone: str) -> dict:
    """返回指定时区的当前时间"""
    now = datetime.now(ZoneInfo(timezone))
    return {"timezone": timezone, "time": now.strftime("%Y-%m-%d %H:%M:%S")}


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    # 只允许数字和四则运算，避免直接 eval() 带来的安全问题
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("只支持数字和 + - * / ** %")


def calculate(expression: str) -> dict:
    """计算一个数学表达式"""
    value = _safe_eval(ast.parse(expression, mode="eval").body)
    return {"expression": expression, "result": value}


def get_current_dir() -> dict:
    """返回当前工作目录"""
    return {"current_dir": str(Path.cwd())}


def get_accounts() -> dict:
    """返回已配置的邮箱账号列表"""
    accounts = []
    with open("accounts.json", encoding="utf-8") as f:
        for a in json.load(f).get("accounts", []):
            if a.get("enabled", True):
                accounts.append({"alias": a["alias"], "email": a["email"], "description": a.get("description", "")})
    return {"accounts": accounts}


CLASSIFY_FILE = Path("state") / "classified.json"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _load_all_mail() -> dict:
    """读 data/ 下所有拉取结果，按 "别名:邮件ID" 建索引"""
    mails = {}
    for p in sorted(Path(fetch_mail.DATA_DIR).glob("*.json")):
        for m in fetch_mail.load_json(p, {}).get("messages", []):
            mails[m["key"]] = m
    return mails


def fetch_new_mail(account: str | None = None) -> dict:
    """拉取新邮件，只返回数量统计"""
    args = argparse.Namespace(days=2, max=200, account=account, reset=False)
    mails, out_path = fetch_mail.fetch_all(args)
    by_account = {}
    for m in mails:
        by_account[m["account"]] = by_account.get(m["account"], 0) + 1
    return {"total": len(mails), "by_account": by_account, "saved_to": out_path}


def classify_one(mail: dict) -> dict:
    """用 AI 给一封邮件定优先级"""
    prompt = (
        "你是邮件助理。判断这封邮件需要我处理的优先级，只输出 JSON："
        '{"priority": "high|medium|low", "summary": "一句话说明要做什么或这是什么"}\n'
        "high=需要尽快回复或处理；medium=需要处理但不急；low=通知、广告、无需处理。\n\n"
        f"发件人：{mail['from']}\n主题：{mail['subject']}\n正文：\n{mail['body'][:2000]}"
    )
    resp = client.models.generate_content(
        model=MODEL, contents=prompt, config={"response_mime_type": "application/json"})
    result = json.loads(resp.text)
    if result.get("priority") not in PRIORITY_ORDER:
        result["priority"] = "medium"
    return result


def classify_pending(limit: int = 30) -> dict:
    """对还没分类的邮件跑 AI 分类，返回各优先级数量"""
    classified = fetch_mail.load_json(CLASSIFY_FILE, {})
    all_mail = _load_all_mail()
    pending = [m for k, m in all_mail.items() if k not in classified][:limit]
    counts, errors = {p: 0 for p in PRIORITY_ORDER}, 0
    for m in pending:
        try:
            r = classify_one(m)
        except Exception as e:
            print(f"  [分类失败] {m['key']}：{e}")
            errors += 1
            continue
        classified[m["key"]] = {
            "priority": r["priority"], "summary": r.get("summary", ""),
            "account": m["account"], "subject": m["subject"], "from": m["from"],
            "received_at": m["received_at"],
        }
        counts[r["priority"]] += 1
    fetch_mail.save_private(str(CLASSIFY_FILE), classified)
    return {"classified": sum(counts.values()), "by_priority": counts, "errors": errors,
            "remaining": len(all_mail) - len(classified)}


_last_listing: dict[int, str] = {}   # 最近一次 list_todos 的 序号 -> 邮件 key


def list_todos(account: str | None = None, priority: str | None = None,
               since: str | None = None) -> dict:
    """按优先级返回待办（带序号），可按账号、优先级、日期（YYYY-MM-DD 起）筛选"""
    todos = []
    for key, c in fetch_mail.load_json(CLASSIFY_FILE, {}).items():
        if c.get("done"):
            continue
        if account and c["account"] != account:
            continue
        if priority and c["priority"] != priority:
            continue
        if since and c["received_at"] < since:
            continue
        todos.append({"key": key, **c})
    todos.sort(key=lambda t: t["received_at"], reverse=True)
    todos.sort(key=lambda t: PRIORITY_ORDER[t["priority"]])
    _last_listing.clear()
    for no, t in enumerate(todos, 1):
        t["no"] = no
        _last_listing[no] = t["key"]
    return {"count": len(todos), "todos": todos}


def keep_todos(keep: list[int]) -> dict:
    """在最近一次 list_todos 的结果里，只保留 keep 中的序号，其余标记为已删除"""
    if not _last_listing:
        return {"error": "还没有列出过待办，请先调用 list_todos"}
    unknown = [n for n in keep if n not in _last_listing]
    if unknown:
        return {"error": f"这些序号不存在：{unknown}"}
    # 不从 classified.json 里删掉条目，否则 classify_pending 会把它们重新分类
    classified = fetch_mail.load_json(CLASSIFY_FILE, {})
    removed = 0
    for no, key in _last_listing.items():
        if no not in keep and key in classified:
            classified[key]["done"] = True
            removed += 1
    fetch_mail.save_private(str(CLASSIFY_FILE), classified)
    _last_listing.clear()                      # 序号已失效，下次需重新列出
    return {"kept": sorted(keep), "removed": removed}


def confirm(call) -> bool:
    print(f"\n⚠️  agent 希望：{call.name}")
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2))
    answer = input("允许吗？输入 y 确认，其他任意键拒绝：").strip().lower()
    return answer == "y"


TOOL_IMPLS = {
    "get_current_time": get_current_time,
    "calculate": calculate,
    "get_current_dir": get_current_dir,
    "get_accounts": get_accounts,
    "fetch_new_mail": fetch_new_mail,
    "classify_pending": classify_pending,
    "list_todos": list_todos,
    "keep_todos": keep_todos,
}

DANGEROUS_TOOLS = {
    "keep_todos",
}
# ---------------------------------------------------------------------------
# 2. 工具说明书
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Get the current date and time in a given IANA timezone.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, e.g. Asia/Shanghai, America/New_York",
                },
            },
            "required": ["timezone"],
        },
    },
    {
        "type": "function",
        "name": "calculate",
        "description": "Evaluate an arithmetic expression. Use this instead of doing math in your head.",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "e.g. (3 + 4) * 2 ** 3"},
            },
            "required": ["expression"],
        },
    },
    {
        "type": "function",
        "name": "get_current_dir",
        "description": "Return the current working directory.",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
        "name": "get_accounts",
        "description": "List configured mail accounts (alias, email, description).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "fetch_new_mail",
        "description": "Fetch new inbox mail from all accounts (or one alias). Returns counts only.",
        "parameters": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Optional account alias; omit for all accounts"},
            },
        },
    },
    {
        "type": "function",
        "name": "classify_pending",
        "description": "Run AI priority classification on fetched mail not yet classified. Returns counts per priority.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max mails to classify this call, default 30"},
            },
        },
    },
    {
        "type": "function",
        "name": "list_todos",
        "description": "List classified mail sorted by priority (high first). Each todo has a number 'no'; "
                       "always show it to the user. All filters optional.",
        "parameters": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account alias"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "since": {"type": "string", "description": "Only mail received on/after this date, YYYY-MM-DD"},
            },
        },
    },
    {
        "type": "function",
        "name": "keep_todos",
        "description": "Keep only the given numbers ('no') from the most recent list_todos result; "
                       "all other todos in that list are removed. Use when the user picks which todos to keep.",
        "parameters": {
            "type": "object",
            "properties": {
                "keep": {"type": "array", "items": {"type": "integer"},
                         "description": "Todo numbers to keep, e.g. [1, 3, 5]; empty list removes all"},
            },
            "required": ["keep"],
        },
    },
]


# ---------------------------------------------------------------------------
# 3. Agent 循环
# ---------------------------------------------------------------------------

def run_turn(user_text: str, previous_id: str | None) -> tuple[str, str]:
    """处理用户的一句话，返回 (最终回答, 本轮最后一个 interaction id)。"""
    interaction = client.interactions.create(
        model=MODEL,
        input=user_text,
        tools=TOOLS,
        previous_interaction_id=previous_id,   # 接上之前的对话，实现多轮记忆
    )

    for _ in range(MAX_STEPS):
        calls = [s for s in interaction.steps if s.type == "function_call"]
        if not calls:                       # 模型不再要工具 → 它准备好回答了
            return interaction.output_text, interaction.id

        results = []
        for call in calls:                  # 模型可能一次要求调用多个工具
            print(f"  [工具] {call.name}({call.arguments})")
            if call.name in DANGEROUS_TOOLS and not confirm(call):
                output = {"error": "用户拒绝了这个操作。不要重试，先问用户想怎么调整。"}
            else:
                try:
                    output = TOOL_IMPLS[call.name](**call.arguments)
                except Exception as e:
                    output = {"error": str(e)}
            print(f"  [结果] {output}")
            results.append({
                "type": "function_result",
                "name": call.name,
                "call_id": call.id,
                "result": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
            })

        # 把工具结果送回模型，让它继续想
        interaction = client.interactions.create(
            model=MODEL,
            input=results,
            tools=TOOLS,
            previous_interaction_id=interaction.id,
        )

    return "（超过最大步数，已停止）", interaction.id


def main():
    print("输入问题，回车发送；输入 exit 退出。")
    previous_id = None 
    SHORTCUTS = {
                "新邮件":"拉取上次拉取后新收到的邮件，返回的报告按照账号和重要性分类，报告中只包含新拉取的邮件（验证码邮件视为不重要）",
                "待办":"根据已拉取的邮件列出所有待办任务，按优先级排序，每条前面标上序号 no",
            }
    while True:
        user_text = input("你> ").strip()
        if user_text.lower() in {"exit", "quit"}:
            break
        if not user_text:
            continue
        if user_text in SHORTCUTS:
            user_text = SHORTCUTS[user_text]
        answer, previous_id = run_turn(user_text, previous_id)
        print(f"Agent> {answer}\n")


if __name__ == "__main__":
    main()
