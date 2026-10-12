"""首次授权小程序：为 accounts.json 里的 Gmail 账号逐个授权，并把令牌存到 tokens/别名.json。

用法（在项目文件夹里运行）：
    python authorize.py            为所有"已启用但还没有令牌"的账号授权
    python authorize.py work       只为别名是 work 的账号（重新）授权

基于 Google 官方 Gmail Python 快速入门示例修改：
  1. 权限改为 gmail.modify（快速入门用的是只读权限）
  2. 每个账号单独保存一个令牌文件，而不是共用 token.json
  3. 授权时强制弹出账号选择，并提示目标邮箱
  4. 授权后用 getProfile 核对邮箱、检查权限，不对就不保存
"""

import json
import os
import sys
from datetime import datetime

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = [SCOPE]
ACCOUNTS_FILE = "accounts.json"
CREDENTIALS_FILE = "credentials.json"
TOKENS_DIR = "tokens"
AUTH_LOG = os.path.join(TOKENS_DIR, "authorized_dates.json")


def load_accounts():
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        accounts = json.load(f)["accounts"]
    aliases = [a["alias"] for a in accounts]
    if len(aliases) != len(set(aliases)):
        sys.exit("accounts.json 里有重复的别名，请先修正。")
    return [a for a in accounts if a.get("enabled", True)]


def token_path(alias):
    return os.path.join(TOKENS_DIR, f"{alias}.json")


def save_private(path, text):
    """写文件，并在 Mac/Linux 上设成只有自己能读写（Windows 上权限设置不生效）。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def record_auth_date(alias):
    log = {}
    if os.path.exists(AUTH_LOG):
        with open(AUTH_LOG, encoding="utf-8") as f:
            log = json.load(f)
    log[alias] = datetime.now().isoformat(timespec="seconds")
    save_private(AUTH_LOG, json.dumps(log, ensure_ascii=False, indent=2))


def authorize(account):
    alias, email = account["alias"], account["email"]
    print(f"\n=== 正在授权：{alias}（{email}）===")
    print("浏览器会打开授权页。请务必选择上面这个邮箱。")
    print("如果浏览器总是自动选错账号，请把终端里打印的链接复制到无痕窗口里打开。\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(
        port=0,  # 与官方快速入门相同：随机选一个空闲端口
        prompt="select_account consent",  # 强制选择账号，并重新显示同意页
        login_hint=email,  # 提示 Google 预选该邮箱（不保证生效，仍需自己看清）
    )

    # 检查 1：权限。Google 允许用户只同意部分权限；
    # granted_scopes 为空表示与申请的一致。
    granted = getattr(creds, "granted_scopes", None)
    if granted and SCOPE not in granted:
        print(f"[失败] 没有拿到 gmail.modify 权限（实际授予：{granted}）。")
        print("请重新运行，并在同意页勾选所有权限。令牌未保存。")
        return False

    # 检查 2：邮箱。用 getProfile 确认这份令牌属于哪个账号。
    service = build("gmail", "v1", credentials=creds)
    actual = service.users().getProfile(userId="me").execute()["emailAddress"]
    if actual.lower() != email.lower():
        print(f"[失败] 授权的是 {actual}，不是 {email}。令牌未保存，请重新运行并选对账号。")
        return False

    # 检查通过：保存令牌，记录授权日期
    save_private(token_path(alias), creds.to_json())
    record_auth_date(alias)
    print(f"[成功] {alias} 已授权，令牌保存在 {token_path(alias)}")
    print("提醒：项目处于测试状态时，授权约 7 天后过期，届时重新运行本程序即可。")
    return True


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(f"找不到 {CREDENTIALS_FILE}，请把第 2 天下载的客户端文件放到当前文件夹。")
    os.makedirs(TOKENS_DIR, exist_ok=True)
    accounts = load_accounts()

    if len(sys.argv) > 1:
        targets = [a for a in accounts if a["alias"] == sys.argv[1]]
        if not targets:
            sys.exit(f"accounts.json 里没有已启用的别名：{sys.argv[1]}")
    else:
        targets = [a for a in accounts if not os.path.exists(token_path(a["alias"]))]
        if not targets:
            print("所有已启用的账号都已有令牌。要重新授权某个账号，请运行：python authorize.py 别名")
            return

    results = {a["alias"]: authorize(a) for a in targets}
    print("\n=== 汇总 ===")
    for alias, ok in results.items():
        print(f"{alias}: {'成功' if ok else '失败，需要重做'}")


if __name__ == "__main__":
    main()
