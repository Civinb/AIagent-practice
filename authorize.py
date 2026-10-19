"""First-time authorization helper: authorize each Gmail account in accounts.json one by one and
save its token to tokens/<alias>.json.

Usage (run inside the project folder):
    python authorize.py            Authorize every account that is enabled but has no token yet
    python authorize.py work       (Re)authorize only the account whose alias is work

Adapted from Google's official Gmail Python quickstart:
  1. The scope is gmail.modify (the quickstart uses a read-only scope)
  2. Each account gets its own token file instead of a shared token.json
  3. Authorization forces the account chooser and shows the target mailbox
  4. After authorizing, getProfile checks the mailbox and the scope; nothing is saved if they are wrong
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
        sys.exit("accounts.json has duplicate aliases; please fix them first.")
    return [a for a in accounts if a.get("enabled", True)]


def token_path(alias):
    return os.path.join(TOKENS_DIR, f"{alias}.json")


def save_private(path, text):
    """Write a file and, on Mac/Linux, make it readable and writable only by you
    (the permission setting has no effect on Windows)."""
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
    print(f"\n=== Authorizing: {alias} ({email}) ===")
    print("Your browser will open the authorization page. Make sure you choose the mailbox above.")
    print("If the browser keeps picking the wrong account, copy the link printed in the terminal "
          "into a private window.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(
        port=0,  # Same as the official quickstart: pick a random free port
        prompt="select_account consent",  # Force the account chooser and show the consent page again
        login_hint=email,  # Ask Google to preselect this mailbox (not guaranteed; still check it yourself)
    )

    # Check 1: scope. Google lets users grant only some of the requested scopes;
    # an empty granted_scopes means the grant matches the request.
    granted = getattr(creds, "granted_scopes", None)
    if granted and SCOPE not in granted:
        print(f"[FAILED] The gmail.modify scope was not granted (actually granted: {granted}).")
        print("Run again and tick every permission on the consent page. Token not saved.")
        return False

    # Check 2: mailbox. Use getProfile to confirm which account this token belongs to.
    service = build("gmail", "v1", credentials=creds)
    actual = service.users().getProfile(userId="me").execute()["emailAddress"]
    if actual.lower() != email.lower():
        print(f"[FAILED] You authorized {actual}, not {email}. Token not saved; "
              f"run again and choose the right account.")
        return False

    # Checks passed: save the token and record the authorization date
    save_private(token_path(alias), creds.to_json())
    record_auth_date(alias)
    print(f"[OK] {alias} is authorized; token saved to {token_path(alias)}")
    print("Reminder: while the project is in testing mode, authorization expires after about "
          "7 days; just run this program again then.")
    return True


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(f"{CREDENTIALS_FILE} not found. Put the OAuth client file you downloaded from "
                 f"Google Cloud into this folder.")
    os.makedirs(TOKENS_DIR, exist_ok=True)
    accounts = load_accounts()

    if len(sys.argv) > 1:
        targets = [a for a in accounts if a["alias"] == sys.argv[1]]
        if not targets:
            sys.exit(f"No enabled account with this alias in accounts.json: {sys.argv[1]}")
    else:
        targets = [a for a in accounts if not os.path.exists(token_path(a["alias"]))]
        if not targets:
            print("Every enabled account already has a token. To reauthorize an account, run: "
                  "python authorize.py <alias>")
            return

    results = {a["alias"]: authorize(a) for a in targets}
    print("\n=== Summary ===")
    for alias, ok in results.items():
        print(f"{alias}: {'OK' if ok else 'FAILED, please redo'}")


if __name__ == "__main__":
    main()
