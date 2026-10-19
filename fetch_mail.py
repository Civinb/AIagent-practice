"""Fetch mail: pull new inbox mail from every enabled account in accounts.json, clean it up and
save it to the database mail.db.

Prerequisite: every account has been authorized with authorize.py (tokens/<alias>.json exists).

Usage (run inside the project folder):
    python fetch_mail.py               If the database has no mail for an account, fetch the last 2 days;
                                       after that, only fetch mail newer than the newest one in the database
    python fetch_mail.py --days 1      If the database has no mail for an account, fetch the last 1 day
    python fetch_mail.py --max 50      Process at most 50 emails per account (default 200)

Other programs (such as the web UI) call fetch_all(), which returns a summary dict and prints nothing.

Only mail in the inbox's Primary tab is fetched; Social, Promotions, Updates and Forums are skipped.
This program only reads mail. It never modifies, moves, deletes or labels any email.
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

OVERLAP_SECONDS = 3600     # Go back an extra hour on incremental fetches; better to fetch extra and dedupe than to miss mail
MAX_BODY_CHARS = 4000      # Maximum number of body characters kept
MAX_ERROR_CHARS = 200      # Maximum characters of an error message (summary only; no tokens or body text)


# ---------------------------------------------------------------- file reading

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_accounts():
    """Read accounts.json and return the enabled accounts (accounts without "enabled" count as enabled)."""
    data = load_json(ACCOUNTS_FILE, {"accounts": []})
    return [a for a in data.get("accounts", []) if a.get("enabled", True)]


# ---------------------------------------------------------------- connecting to Gmail

def connect(account):
    """Load the account's token, connect to Gmail and check the mailbox address.

    Returns (service, None); on failure returns (None, error message).
    """
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    alias, expected = account["alias"], account["email"]
    path = os.path.join(TOKENS_DIR, f"{alias}.json")
    if not os.path.exists(path):
        return None, f"{alias}: no token; run python authorize.py {alias} first"

    creds = Credentials.from_authorized_user_file(path, SCOPES)
    service = build("gmail", "v1", credentials=creds)
    try:
        # When the access token has expired, the library swaps in a new one using the refresh token
        actual = service.users().getProfile(userId="me").execute()["emailAddress"]
    except RefreshError:
        return None, (f"{alias}: authorization has expired (about 7 days in testing mode); "
                      f"run python authorize.py {alias} to authorize again")

    if actual.lower() != expected.lower():
        return None, (f"{alias}: the token belongs to {actual}, not {expected} as listed; "
                      f"please authorize again")
    return service, None


# ---------------------------------------------------------------- step 1: get the ID list

def list_message_ids(service, query, limit):
    """List matching inbox message IDs (newest first), following pages, at most limit of them."""
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


# ---------------------------------------------------------------- step 2: read and clean the content

class _HTMLToText(HTMLParser):
    """Turn an HTML email into plain text: drop styles and scripts, break lines at block tags."""
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


# A line matching one of these means everything below it is quoted earlier mail, so cut it off.
# The Chinese text in the patterns is intentional: it matches headers that Gmail writes into
# Chinese-language emails, so it must stay in Chinese.
_QUOTE_HEADERS = [
    re.compile(r"^On .+wrote:\s*$"),                        # English Gmail
    re.compile(r".*写道[:：]\s*$"),                          # Chinese Gmail ("... wrote:")
    re.compile(r"^-{2,}\s*(Original Message|原始邮件)\s*-{2,}", re.I),
]
# Note: "Forwarded message" is not cut off, because the forwarded content is often the point of the email


def clean_body(text):
    """Remove quoted replies and signatures, collapse blank lines, truncate long content.
    These are rules of thumb and not guaranteed to be perfect."""
    text = text.replace("\r\n", "\n").replace("\xa0", " ").replace("​", "")
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue                                        # Quoted line
        if stripped == "--":
            break                                           # Standard signature separator "-- "
        if any(p.match(stripped) for p in _QUOTE_HEADERS) and kept:
            break                                           # Everything below is earlier mail
        kept.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n... (body too long, truncated)"
    return text


def extract_body(msg):
    """Prefer the plain-text body; fall back to HTML converted to text. Python's email library
    handles encodings and character sets automatically."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):              # Fallback when the declared charset is wrong
        content = part.get_payload(decode=True).decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return clean_body(content)


def fetch_one(service, alias, msg_id):
    """Read one email and return it as a cleaned-up dict."""
    resp = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
    raw_bytes = base64.urlsafe_b64decode(resp["raw"])
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    internal_ms = int(resp.get("internalDate", 0))
    return {
        "key": f"{alias}:{msg_id}",           # Alias + message ID as the unique key
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


# ---------------------------------------------------------------- main flow

def build_query(alias, days):
    """If the database has mail for this account, only fetch mail after the newest one; otherwise
    fetch the last days days. Only the Primary tab is fetched."""
    last_ms = db.latest_internal_ms(alias)
    if last_ms:
        # Use a timestamp in seconds so Gmail does not interpret the date as midnight Pacific time
        return f"after:{last_ms // 1000 - OVERLAP_SECONDS} category:primary"
    return f"newer_than:{days}d category:primary"


def list_new_ids(service, alias, days, limit):
    """List this account's matching message IDs, skipping ones already in the database.

    Returns (total found, list of new message IDs). New IDs are ordered oldest to newest, so if
    the run is interrupted, the database's "newest email" never jumps ahead of unread ones and
    the next incremental fetch will not miss them.
    """
    ids = list_message_ids(service, build_query(alias, days), limit)
    new_ids = [i for i in ids if not db.email_exists(f"{alias}:{i}")]
    new_ids.reverse()
    return len(ids), new_ids


def save_one(service, alias, msg_id):
    """Read one email and save it immediately. Returns whether a new row was actually added."""
    return db.insert_email(fetch_one(service, alias, msg_id))


def describe_error(alias, err):
    """Turn an exception into a short one-line message (exception type and a short description only)."""
    return f"{alias}: error ({type(err).__name__}): {str(err)[:MAX_ERROR_CHARS]}"


def prepare_account(account, days, max_per_account):
    """Connect to one account and list its new message IDs.

    Returns (service, total found, list of new message IDs, list of problems); service is None
    if the connection failed.
    """
    alias = account["alias"]
    try:
        service, error = connect(account)
        if service is None:
            return None, 0, [], [error]
        found, new_ids = list_new_ids(service, alias, days, max_per_account)
    except Exception as e:                       # Network, corrupt token file, etc.: record it and move on to the other accounts
        return None, 0, [], [describe_error(alias, e)]

    problems = []
    if found >= max_per_account:
        problems.append(f"{alias}: reached the per-run limit of {max_per_account} emails; "
                        f"older mail may not have been fetched")
    return service, found, new_ids, problems


def fetch_all(days=2, max_per_account=200, progress=None):
    """Fetch new mail for all enabled accounts, save it, and return a summary:

        {"accounts": {alias: {"found": found, "new": newly saved, "failed": failed to read}},
         "problems": ["work: authorization has expired, ...", ...]}

    "accounts" only contains accounts that connected and listed mail successfully; accounts that
    failed to connect only appear in "problems".
    progress is an optional callback progress(fraction, message), called once after each email;
    fraction is between 0 and 1 and covers all accounts. Never interrupted by an exception;
    every problem is recorded in "problems".
    """
    db.init_db()
    summary = {"accounts": {}, "problems": []}
    try:
        accounts = load_accounts()
    except (OSError, ValueError) as e:           # File unreadable or invalid JSON
        summary["problems"].append(f"Failed to read {ACCOUNTS_FILE} ({type(e).__name__}): {e}")
        return summary
    if not accounts:
        summary["problems"].append(f"No enabled accounts in {ACCOUNTS_FILE}")
        return summary

    # First list the new message IDs of every account to get the total, so progress can be computed
    jobs = []
    for account in accounts:
        service, found, new_ids, problems = prepare_account(account, days, max_per_account)
        summary["problems"].extend(problems)
        if service is not None:
            summary["accounts"][account["alias"]] = {"found": found, "new": 0, "failed": 0}
            jobs.append((account["alias"], service, new_ids))

    # Then read them one by one, saving each email as soon as it is read
    total = sum(len(new_ids) for _, _, new_ids in jobs)
    done = 0
    for alias, service, new_ids in jobs:
        stats = summary["accounts"][alias]
        for msg_id in new_ids:
            try:
                if save_one(service, alias, msg_id):
                    stats["new"] += 1
            except Exception:                    # One failed email does not affect the others
                stats["failed"] += 1
            done += 1
            if progress is not None:
                progress(done / total, f"{alias}: processed {done}/{total} emails")
    return summary


# ---------------------------------------------------------------- command-line entry point

def format_summary(summary):
    """Format the summary dict as a few lines of text for printing on the command line."""
    lines = [f"[{alias}] found {s['found']}, new {s['new']}, failed {s['failed']}"
             for alias, s in summary["accounts"].items()]
    if summary["problems"]:
        lines.append("Problems:")
        lines.extend(f"  - {p}" for p in summary["problems"])
    return "\n".join(lines) if lines else "No accounts were processed."


def main():
    ap = argparse.ArgumentParser(description="Fetch new inbox mail from several Gmail accounts into mail.db")
    ap.add_argument("--days", type=int, default=2,
                    help="If the database has no mail for an account, how many days back to fetch (default 2)")
    ap.add_argument("--max", type=int, default=200,
                    help="Maximum emails to process per account in one run (default 200)")
    args = ap.parse_args()
    print(format_summary(fetch_all(days=args.days, max_per_account=args.max)))


if __name__ == "__main__":
    main()
