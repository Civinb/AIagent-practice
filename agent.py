"""
Gemini agent

运行前：
    pip install -U google-genai
    export GEMINI_API_KEY="你的key"      # Windows PowerShell: $env:GEMINI_API_KEY="你的key"
    python agent.py
"""

import ast
import json
import operator
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from google import genai

MODEL = "gemini-3.5-flash-lite"   # 官方文档示例用的模型；想省额度可换 gemini-3.5-flash-lite
MAX_STEPS = 8                # 防止模型无限调用工具

client = genai.Client()      # 会从环境变量 GEMINI_API_KEY 读取 key


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


def list_rename_files(path:str) -> dict:
    # 找出目录下带有.弹幕后缀的弹幕文件，返回文件路径
    files = []
    p = Path(path)
    files = [str(p) for p in p.rglob("*.xml") if ".弹幕" in p.name and p.is_file()]
    return {"files": files}


def get_current_dir() -> dict:
    """返回当前工作目录"""
    return {"current_dir": str(Path.cwd())}


def create_rename_mapping(files: list[str]) -> dict:
    """为每个文件生成新的重命名路径"""
    mapping = []
    for file in files:
        old_path = Path(file)
        new_name = old_path.stem.replace(".弹幕", "") + old_path.suffix
        new_path = old_path.with_name(new_name)
        mapping.append({"old_path": str(old_path), "new_path": str(new_path)})
    return {"rename_mapping": mapping}


def rename_files(mapping: list[dict]) -> dict:
    """重命名文件"""
    for item in mapping:
        old_path = Path(item["old_path"])
        new_path = Path(item["new_path"])
        if not old_path.exists():
            raise FileNotFoundError(f"文件不存在: {item['old_path']}")
        old_path.rename(new_path)
    return {"status": "success."}


def create_rename_logfile(mapping: list[dict]):
    """生成重命名日志"""
    log = []
    for item in mapping:
        log.append(f"Renamed: {item['old_path']} -> {item['new_path']}")
    log_path = Path("rename_log.txt")
    log_path.write_text("\n".join(log), encoding="utf-8")
    return {"logfile": str(log_path)}


def confirm(call) -> bool:
    print(f"\n⚠️  agent 希望：{call.name}")
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2))
    answer = input("允许吗？输入 y 确认，其他任意键拒绝：").strip().lower()
    return answer == "y"


TOOL_IMPLS = {
    "get_current_time": get_current_time,
    "calculate": calculate,
    "list_rename_files": list_rename_files,
    "get_current_dir": get_current_dir,
    "create_rename_mapping": create_rename_mapping,
    "rename_files": rename_files,
    "create_rename_logfile": create_rename_logfile,
}

DANGEROUS_TOOLS = {
    "rename_files",
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
        "name": "list_rename_files",
        "description": "List all files with .弹幕 suffix in a given directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list files in."},
            },
            "required": ["path"],
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
        "name": "create_rename_mapping",
        "description": "Generate new file names for a list of files to be renamed.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to be renamed.",
                },
            },
            "required": ["files"],
        },
    },
    {
        "type": "function",
        "name": "rename_files",
        "description": "Rename files according to a provided mapping.",
        "parameters": {
            "type": "object",
            "properties": {
                "mapping": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_path": {"type": "string"},
                            "new_path": {"type": "string"},
                        },
                        "required": ["old_path", "new_path"],
                    },
                    "description": "List of old and new file paths for renaming.",
                },
            },
            "required": ["mapping"],
        },
    },
    {
        "type": "function",
        "name": "create_rename_logfile",
        "description": "Create a log file documenting the renaming of files.",
        "parameters": {
            "type": "object",
            "properties": {
                "mapping": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_path": {"type": "string"},
                            "new_path": {"type": "string"},
                        },
                        "required": ["old_path", "new_path"],
                    },
                },
            },
            "required": ["mapping"],
        },
    }
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
                "重命名": "把这个目录下的所有 .弹幕 文件重命名为去掉 .弹幕 后缀的文件名，并生成一个重命名日志文件。",
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
