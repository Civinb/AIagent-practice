"""Web UI: triage mail in the local browser with Gradio Blocks.

Usage (run inside the project folder; fetch_mail.py reads accounts.json and tokens/ by relative path):
    python app.py
Then open the address shown in the terminal (it only listens on 127.0.0.1 and is not public).

Three tabs:
    Triage     Fetch and analyze, filter, tick emails to add to to-dos or discard, view one email's details
    To-dos     View open to-dos (with urgency marks), mark done, delete, edit title/deadline/note, undo done
    Discarded  View recently discarded emails and restore ticked ones to review
The UI only reads and writes the local database mail.db. It never sends, deletes or modifies any Gmail message.
"""

import re
from datetime import date, datetime
from email.utils import parseaddr

import gradio as gr

import analyze
import db
import fetch_mail

# ---------------------------------------------------------------- constants

ALL = "All"                                        # The "no filter" option in filter dropdowns
PRIORITY_CHOICES = [ALL, "P1", "P2", "P3", "P4"]
DEFAULT_CHECKED_PRIORITIES = (1, 2)                # Ticked by default: P1/P2 that need action
SUBJECT_MAX_CHARS = 30                             # Maximum subject characters shown in the list
SENDER_MAX_CHARS = 20                              # Maximum sender-name characters shown in the list
MAX_ERROR_CHARS = 200                              # Maximum error-message characters shown in status text

FETCH_SHARE = 0.5                                  # Progress bar: first half for fetching, second half for analysis

# To-dos tab
SORT_CHOICES = [("By deadline", db.SORT_DEADLINE), ("By priority", db.SORT_PRIORITY)]
DONE_LIMIT = 30                                    # Maximum number of recently completed items shown under "Done"
URGENT_DAYS = 3                                    # Items with 1–3 days left show "🟡 N days left"
TITLE_MAX_CHARS = 40                               # Maximum title characters shown in the to-do list

# Discarded tab
DISCARDED_LIMIT = 100                              # Maximum number of recently discarded emails shown

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")   # Characters that must be escaped in Markdown
_DATE_FORMAT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")  # A deadline must be exactly YYYY-MM-DD


# ---------------------------------------------------------------- text helpers

def md_escape(text):
    """Escape Markdown special characters so email content (untrusted) cannot render as links, images, etc."""
    return _MD_SPECIAL.sub(r"\\\1", "" if text is None else str(text))


def code_block(text):
    """Put the body verbatim in a code block; the fence is longer than the longest run of backticks
    in the body, so the body cannot end the code block early."""
    text = text or ""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def truncate(text, limit):
    """Truncate text longer than limit characters and add an ellipsis."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def sender_name(sender):
    """Sender display name: the name if there is one, otherwise the email address."""
    name, address = parseaddr(sender or "")
    return name or address or "(unknown sender)"


def short_date(deadline):
    """YYYY-MM-DD → 10/17; returned unchanged if the format is wrong."""
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d")
    except (TypeError, ValueError):
        return deadline
    return f"{d.month}/{d.day}"


def describe_error(err):
    """Short error description: type + the first MAX_ERROR_CHARS characters."""
    return f"{type(err).__name__}: {str(err)[:MAX_ERROR_CHARS]}"


# ---------------------------------------------------------------- filters

def account_filter(value):
    """Account dropdown value → the account argument of db.list_emails ("All" is None)."""
    return None if value in (None, ALL) else value


def priority_filter(value):
    """Priority dropdown value → the priority argument of db.list_emails ("P1" → 1, "All" is None)."""
    return None if value in (None, ALL) else int(value[1:])


def describe_filter(account, priority):
    """Text description of the filter, e.g. "account work" or "all accounts, priority P1"."""
    text = "all accounts" if account_filter(account) is None else f"account {account}"
    if priority_filter(priority) is not None:
        text += f", priority {priority}"
    return text


def account_aliases():
    """List of account aliases: enabled accounts in accounts.json first, then accounts that have
    mail in the database but are not in the list."""
    try:
        aliases = [a["alias"] for a in fetch_mail.load_accounts()]
    except (OSError, ValueError, KeyError):         # If the list cannot be read, use only the database's accounts
        aliases = []
    for alias in db.count_by_status():
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def list_review(account, priority):
    """Emails to review under the current filter."""
    return db.list_emails(db.REVIEW, account=account_filter(account),
                          priority=priority_filter(priority))


def only_review(keys):
    """Keep only keys that are still in review now (the UI may be stale; avoids changing an
    already handled email again)."""
    review = {e["key"] for e in db.list_emails(db.REVIEW)}
    return [k for k in keys if k in review]


# ---------------------------------------------------------------- list display

def analysis_failed(email):
    """Emails whose analysis failed (or that have no analysis record)."""
    a = email["analysis"]
    return a is None or bool(a["error"])


def email_label(email):
    """Display text for one email in the review list."""
    sender = truncate(sender_name(email["sender"]), SENDER_MAX_CHARS)
    subject = truncate(email["subject"], SUBJECT_MAX_CHARS) or "(no subject)"
    if analysis_failed(email):
        return f"⚠ Analysis failed | {email['account']} | {sender} | {subject}"

    a = email["analysis"]
    text = subject
    if a["action"]:
        text += f" → {a['action']}"
    if a["deadline"]:
        text += f" (due {short_date(a['deadline'])})"
    label = f"P{a['priority']} | {email['account']} | {sender} | {text}"
    return "⚠ Possible phishing " + label if a["suspicious"] else label


def default_checked(email):
    """Whether it is ticked by default: analysis succeeded, priority P1/P2, needs action."""
    if analysis_failed(email):
        return False
    a = email["analysis"]
    return a["priority"] in DEFAULT_CHECKED_PRIORITIES and bool(a["needs_action"])


def status_text(message=""):
    """Status text: number of emails to review per account + the result of the last action."""
    counts = db.count_by_status()
    per_account = [(alias, counts.get(alias, {}).get(db.REVIEW, 0)) for alias in account_aliases()]
    total = sum(n for _, n in per_account)
    parts = ", ".join(f"{md_escape(alias)} {n}" for alias, n in per_account) or "no accounts"
    text = f"**To review**: {parts} ({total} in total)"
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- details

def detail_text(key):
    """Markdown details of one email to review; a hint if nothing is selected or it is no longer in review."""
    if not key:
        return "Select an email to view its details."
    email = db.get_email(key)
    if email is None or email["status"] != db.REVIEW:
        return "This email is no longer in the review list."

    a = email["analysis"] or {}
    attachments = ", ".join(md_escape(n) for n in email["attachments"]) or "none"
    lines = [
        f"### {md_escape(email['subject']) or '(no subject)'}",
        "",
        f"- Account: {md_escape(email['account'])}",
        f"- From: {md_escape(email['sender'])}",
        f"- Received: {md_escape(email['received_at'])}",
    ]
    if analysis_failed(email):
        lines += [f"- **⚠ Analysis failed**: {md_escape(a.get('error') or 'no analysis record')}"]
    else:
        lines += [
            f"- Priority: P{a['priority']} · Category: {md_escape(a['category'])}"
            f" · Needs action: {'yes' if a['needs_action'] else 'no'}",
            f"- What to do: {md_escape(a['action']) or 'nothing'}",
            f"- Deadline: {md_escape(a['deadline']) or 'none'}",
        ]
        if a["suspicious"]:
            lines += ["- **⚠ Possible phishing**"]
    lines += [f"- Attachments: {attachments}"]
    if not analysis_failed(email):
        lines += ["", f"**AI summary**: {md_escape(a['summary'])}",
                  "", f"**Reasoning**: {md_escape(a['reason'])}"]
    lines += ["", "**Body**", "", code_block(email["body"])]
    return "\n".join(lines)


# ---------------------------------------------------------------- refresh

def refresh(account, priority, detail_key, message=""):
    """Rebuild the Triage tab for the current filter; the return order matches SORT_OUTPUTS:

    (review list, status text, details dropdown, details, confirm row, keys awaiting confirmation)
    Ticks in the review list are reset to the defaults; the details dropdown keeps the previously
    selected email when possible; the confirm row is always hidden.
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
    """On error: show the error only in the status text and leave the other outputs unchanged."""
    return (gr.skip(), f"**Error**: {md_escape(describe_error(err))}",
            gr.skip(), gr.skip(), gr.skip(), gr.skip())


def message_only(message):
    """Update only the status text (e.g. a button clicked with nothing ticked); other outputs unchanged."""
    return (gr.skip(), status_text(message), gr.skip(), gr.skip(), gr.skip(), gr.skip())


# ---------------------------------------------------------------- fetch and analyze

def scaled_progress(progress, start, share):
    """Map a 0–1 sub-progress onto [start, start + share] of the overall progress bar;
    returns a callback f(fraction, message)."""
    def callback(fraction, message):
        progress(start + share * fraction, desc=message)
    return callback


def pull_message(fetch_summary, analyze_summary):
    """Format the fetch summary and analysis summary as status text."""
    fetched = "; ".join(
        f"{md_escape(alias)} found {s['found']}, new {s['new']}, failed {s['failed']}"
        for alias, s in fetch_summary["accounts"].items()) or "no account connected successfully"
    text = (f"**Fetch**: {fetched}\n\n"
            f"**Analysis**: {analyze_summary['analyzed']} succeeded, {analyze_summary['failed']} failed")
    problems = fetch_summary["problems"] + analyze_summary["errors"]
    if problems:
        text += "\n\n**Problems**:\n" + "\n".join(f"- {md_escape(p)}" for p in problems)
    return text


def on_pull(account, priority, detail_key, progress=gr.Progress()):
    """Fetch new mail for all accounts and analyze it, then refresh."""
    try:
        progress(0, desc="Starting fetch...")
        fetch_summary = fetch_mail.fetch_all(progress=scaled_progress(progress, 0, FETCH_SHARE))
        progress(FETCH_SHARE, desc="Starting analysis...")
        analyze_summary = analyze.analyze_pending(
            progress=scaled_progress(progress, FETCH_SHARE, 1 - FETCH_SHARE))
        return refresh(account, priority, detail_key, pull_message(fetch_summary, analyze_summary))
    except Exception as e:                            # Any exception is shown only in the status text
        return error_outputs(e)


# ---------------------------------------------------------------- other events

def on_refresh(account, priority, detail_key):
    """Refresh when the page opens or a filter changes."""
    try:
        return refresh(account, priority, detail_key)
    except Exception as e:
        return error_outputs(e)


def on_add_todos(selected, account, priority, detail_key):
    """Add the ticked emails to the to-do list."""
    try:
        if not selected:
            return message_only("No emails are ticked; nothing was done.")
        created = db.add_todos(only_review(selected))
        return refresh(account, priority, detail_key, f"Added {created} emails to to-dos.")
    except Exception as e:
        return error_outputs(e)


def on_discard_selected(selected, account, priority, detail_key):
    """Discard the ticked emails."""
    try:
        if not selected:
            return message_only("No emails are ticked; nothing was done.")
        changed = db.set_status(only_review(selected), db.DISCARDED)
        return refresh(account, priority, detail_key, f"Discarded {changed} emails.")
    except Exception as e:
        return error_outputs(e)


def on_ask_discard_rest(selected, account, priority):
    """"Discard all the rest", step 1: work out the unticked keys under the current filter and show the confirm row.

    Returns (confirm row, confirm text, keys awaiting confirmation, status text).
    """
    try:
        chosen = set(selected or [])
        rest = [e["key"] for e in list_review(account, priority) if e["key"] not in chosen]
        if not rest:
            return (gr.Row(visible=False), gr.skip(), None,
                    status_text(f"There are no unticked emails under {describe_filter(account, priority)}; "
                                f"nothing was done."))
        question = (f"This will discard the {len(rest)} unticked emails "
                    f"({md_escape(describe_filter(account, priority))}). Are you sure?")
        return gr.Row(visible=True), question, rest, gr.skip()
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**Error**: {md_escape(describe_error(e))}"


def on_confirm_discard_rest(pending, account, priority, detail_key):
    """"Discard all the rest", step 2: discard the keys recorded in step 1, then hide the confirm row,
    clear the record and refresh."""
    try:
        changed = db.set_status(only_review(pending or []), db.DISCARDED)
        return refresh(account, priority, detail_key, f"Discarded {changed} emails.")
    except Exception as e:
        return error_outputs(e)


def on_cancel_discard_rest():
    """Cancel: only hide the confirm row and clear the record. Returns (confirm row, keys awaiting confirmation)."""
    return gr.Row(visible=False), None


def on_select_detail(key):
    """Show the details when an email is chosen in the details dropdown."""
    try:
        return detail_text(key)
    except Exception as e:
        return f"**Error**: {md_escape(describe_error(e))}"


# ================================================================ To-dos tab

# ---------------------------------------------------------------- dates

def parse_deadline(text):
    """Validate the deadline in the edit box: empty → None; strict YYYY-MM-DD that is a real date →
    returned unchanged.

    Raises ValueError if invalid (e.g. "next Friday", "2026-2-3", "2026-02-30").
    """
    text = (text or "").strip()
    if not text:
        return None
    if not _DATE_FORMAT.match(text):
        raise ValueError("format is not YYYY-MM-DD")
    datetime.strptime(text, "%Y-%m-%d")             # Dates that do not exist, such as 2026-02-30, raise ValueError
    return text


def days_left(deadline, today):
    """Days left until the deadline (0 if due today, negative if overdue); None if there is no
    deadline or the format is wrong."""
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d - today).days


def urgency_mark(deadline, today):
    """Urgency mark: overdue / due today / 1–3 days left; an empty string otherwise or when there is no deadline."""
    n = days_left(deadline, today)
    if n is None or n > URGENT_DAYS:
        return ""
    if n < 0:
        return "🔴 Overdue"
    if n == 0:
        return "🟡 Due today"
    return f"🟡 {n} days left"


# ---------------------------------------------------------------- to-do list display

def todo_email(todo, cache):
    """The to-do's source email (with analysis); None if there is no source or the email is no
    longer in the database. cache avoids repeated queries."""
    key = todo["email_key"]
    if not key:
        return None
    if key not in cache:
        cache[key] = db.get_email(key)
    return cache[key]


def todo_parts(todo, email):
    """The shared part of a to-do's display text: [title, deadline, account · sender]."""
    title = truncate(todo["title"], TITLE_MAX_CHARS) or "(no title)"
    deadline = f"due {short_date(todo['deadline'])}" if todo["deadline"] else "no deadline"
    sender = truncate(sender_name(email["sender"] if email else None), SENDER_MAX_CHARS)
    return [title, deadline, f"{todo['account']} · {sender}"]


def todo_label(todo, email, today):
    """Display text of an open to-do, e.g. "🔴 Overdue | title | due 10/17 | work · sender"."""
    mark = urgency_mark(todo["deadline"], today)
    return " | ".join(([mark] if mark else []) + todo_parts(todo, email))


def done_label(todo, email):
    """Display text of a completed to-do, e.g. "title | due 10/17 | work · sender | done 10/18"."""
    done_day = short_date((todo["done_at"] or "")[:10])
    return " | ".join(todo_parts(todo, email) + [f"done {done_day}"])


def list_open(account, sort):
    """Open to-dos under the current account filter, in the chosen order."""
    return db.list_todos(done=False, account=account_filter(account), sort=sort)


def list_recent_done(account):
    """The DONE_LIMIT most recently completed to-dos under the current account filter (newest completion first)."""
    done = db.list_todos(done=True, account=account_filter(account))
    done.sort(key=lambda t: t["done_at"] or "", reverse=True)
    return done[:DONE_LIMIT]


def only_open(ids):
    """Keep only to-do ids that are still open now (the UI may be stale; avoids deleting completed ones by mistake)."""
    open_ids = {t["id"] for t in db.list_todos(done=False)}
    return [i for i in ids if i in open_ids]


def todo_status_text(account, message=""):
    """To-dos tab status text: number of open items under the current filter, number overdue /
    due within 3 days + the result of the last action."""
    where = "all accounts" if account_filter(account) is None else f"account {account}"
    today = date.today()
    left = [days_left(t["deadline"], today) for t in list_open(account, db.SORT_DEADLINE)]
    overdue = sum(1 for n in left if n is not None and n < 0)
    soon = sum(1 for n in left if n is not None and 0 <= n <= URGENT_DAYS)
    text = (f"**Open**: {len(left)} in {md_escape(where)}\n\n"
            f"🔴 {overdue} overdue · 🟡 {soon} due within {URGENT_DAYS} days")
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- editor

def source_text(todo):
    """Source-email description below the editor: sender, subject, AI summary."""
    email = todo_email(todo, {})
    if email is None:
        return "Source email not found."
    a = email["analysis"]
    summary = md_escape(a["summary"]) if a and not a["error"] and a["summary"] else "none"
    return "\n".join([
        "**Source email**",
        "",
        f"- From: {md_escape(email['sender']) or '(unknown sender)'}",
        f"- Subject: {md_escape(email['subject']) or '(no subject)'}",
        f"- AI summary: {summary}",
    ])


def editor_values(todo_id):
    """The editor's four outputs: (title, deadline, note, source description). Cleared if nothing
    is selected or the to-do no longer exists."""
    todo = db.get_todo(todo_id) if todo_id is not None else None
    if todo is None or todo["done"]:
        return "", "", "", "Select a to-do to edit."
    return todo["title"], todo["deadline"] or "", todo["note"] or "", source_text(todo)


# ---------------------------------------------------------------- refresh

def refresh_todo(account, sort, edit_id, message="", reload_editor=False):
    """Rebuild the To-dos tab; the return order matches outputs in build_todo_tab:

    (open list, edit dropdown, title, deadline, note, source description, done list, confirm row,
     ids awaiting confirmation, status text)
    Ticks in both lists are cleared; the confirm row is hidden. The edit dropdown keeps the
    previously selected to-do when possible: if it is still there, the edit boxes are left alone
    (unsaved changes are not overwritten), and reload_editor=True refills them from the database;
    if it is no longer in the open list, the editor is cleared.
    """
    today = date.today()                            # Urgency marks use the local date
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
    """On error: show the error only in the status text and leave the other outputs unchanged."""
    return (gr.skip(),) * 9 + (f"**Error**: {md_escape(describe_error(err))}",)


def todo_message_only(account, message):
    """Update only the status text (e.g. a button clicked with nothing ticked); other outputs unchanged."""
    return (gr.skip(),) * 9 + (todo_status_text(account, message),)


# ---------------------------------------------------------------- To-dos tab events

def on_todo_refresh(account, sort, edit_id):
    """Refresh when the page opens, the filter/sort changes, or after "Add to to-dos" on the Triage tab."""
    try:
        return refresh_todo(account, sort, edit_id)
    except Exception as e:
        return todo_error_outputs(e)


def on_mark_done(selected, account, sort, edit_id):
    """Mark the ticked to-dos as done."""
    try:
        if not selected:
            return todo_message_only(account, "No to-dos are ticked; nothing was done.")
        changed = db.mark_done(selected)
        return refresh_todo(account, sort, edit_id, f"Marked {changed} as done.")
    except Exception as e:
        return todo_error_outputs(e)


def on_ask_delete(selected, account):
    """"Delete", step 1: record the ticked ids and show the confirm row.
    Returns (confirm row, confirm text, ids awaiting confirmation, status text)."""
    try:
        if not selected:
            return (gr.Row(visible=False), gr.skip(), None,
                    todo_status_text(account, "No to-dos are ticked; nothing was done."))
        question = (f"This will delete the {len(selected)} ticked to-dos (the to-dos themselves cannot be "
                    f"restored). Their emails move to \"Discarded\", where they can be restored to review. "
                    f"Are you sure?")
        return gr.Row(visible=True), question, list(selected), gr.skip()
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**Error**: {md_escape(describe_error(e))}"


def on_confirm_delete(pending, account, sort, edit_id):
    """"Delete", step 2: delete the ids recorded in step 1 (only those still open), move their emails
    to discarded, then hide the confirm row and refresh."""
    try:
        deleted = db.delete_todos(only_open(pending or []))
        return refresh_todo(account, sort, edit_id,
                            f"Deleted {deleted} to-dos; their emails moved to \"Discarded\".")
    except Exception as e:
        return todo_error_outputs(e)


def on_cancel_delete():
    """Cancel deletion: only hide the confirm row and clear the record. Returns (confirm row, ids awaiting confirmation)."""
    return gr.Row(visible=False), None


def on_select_todo(todo_id):
    """When a to-do is chosen in the edit dropdown, fill the editor with its title, deadline, note and source."""
    try:
        return editor_values(todo_id)
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"**Error**: {md_escape(describe_error(e))}"


def on_save_todo(edit_id, title, deadline, note, account, sort):
    """Save edits: the deadline must be empty or a valid date; if invalid, only show a hint and do
    not save. Only the to-dos table is changed."""
    try:
        if edit_id is None:
            return todo_message_only(account, "Please choose a to-do in the dropdown first.")
        title = (title or "").strip()
        if not title:
            return todo_message_only(account, "The title cannot be empty; not saved.")
        try:
            deadline = parse_deadline(deadline)
        except ValueError:
            return todo_message_only(
                account, f"The deadline \"{md_escape(deadline)}\" is invalid; use YYYY-MM-DD or leave it "
                         f"empty. Not saved.")
        if not db.update_todo(edit_id, title, deadline, (note or "").strip()):
            return refresh_todo(account, sort, None, "This to-do no longer exists; not saved.")
        return refresh_todo(account, sort, edit_id, "Saved.", reload_editor=True)
    except Exception as e:
        return todo_error_outputs(e)


def on_undo_done(selected, account, sort, edit_id):
    """Set the to-dos ticked under "Done" back to open."""
    try:
        if not selected:
            return todo_message_only(account, "No completed to-dos are ticked; nothing was done.")
        changed = db.undo_done(selected)
        return refresh_todo(account, sort, edit_id, f"Undid completion for {changed}.")
    except Exception as e:
        return todo_error_outputs(e)


# ================================================================ Discarded tab

# ---------------------------------------------------------------- list display

def discard_time(email):
    """Display text of the discard time: status_changed_at (ISO format) → 10/18 19:25; returned
    unchanged if the format is wrong."""
    value = email["status_changed_at"]
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "(time unknown)"
    return f"{t.month}/{t.day} {t:%H:%M}"


def discarded_label(email):
    """Display text of one email in the discarded list: discard time + the same text as on the Triage tab."""
    return f"Discarded {discard_time(email)} | {email_label(email)}"


def only_discarded(keys):
    """Keep only keys that are still discarded now (the UI may be stale; avoids changing an
    already handled email again)."""
    discarded = {e["key"] for e in db.list_emails(db.DISCARDED)}
    return [k for k in keys if k in discarded]


def discarded_status_text(message=""):
    """Discarded tab status text: total discarded, number shown in the list + the result of the last action."""
    total = sum(c[db.DISCARDED] for c in db.count_by_status().values())
    text = (f"**Discarded**: {total} in total; the {min(total, DISCARDED_LIMIT)} most recently "
            f"discarded are listed below")
    return text + "\n\n" + message if message else text


# ---------------------------------------------------------------- refresh

def refresh_discarded(message=""):
    """Rebuild the Discarded tab; the return order matches outputs in build_discarded_tab:

    (discarded list, status text)
    The list is sorted by discard time, newest first, at most DISCARDED_LIMIT emails, with ticks cleared.
    """
    emails = db.list_emails(db.DISCARDED, limit=DISCARDED_LIMIT)
    choices = [(discarded_label(e), e["key"]) for e in emails]
    return gr.CheckboxGroup(choices=choices, value=[]), discarded_status_text(message)


def discarded_error_outputs(err):
    """On error: show the error only in the status text and leave the list unchanged."""
    return gr.skip(), f"**Error**: {md_escape(describe_error(err))}"


# ---------------------------------------------------------------- Discarded tab events

def on_discarded_refresh():
    """Refresh when the page opens or after discarding on the Triage tab."""
    try:
        return refresh_discarded()
    except Exception as e:
        return discarded_error_outputs(e)


def on_restore(selected):
    """Restore the ticked discarded emails to review."""
    try:
        if not selected:
            return gr.skip(), discarded_status_text("No emails are ticked; nothing was done.")
        changed = db.set_status(only_discarded(selected), db.REVIEW)
        return refresh_discarded(f"Restored {changed} emails to review.")
    except Exception as e:
        return discarded_error_outputs(e)


# ---------------------------------------------------------------- UI

def build_todo_tab(demo):
    """Build the contents of the "To-dos" tab in place and bind its events.

    Returns {"delete": the "Confirm delete" event, "refresh": the event arguments that refresh this
    tab (fn / inputs / outputs)}, so build_app can refresh the Discarded tab after a deletion and
    refresh this tab after "Add to to-dos" on the Triage tab.
    """
    with gr.Row():
        account_dd = gr.Dropdown([ALL] + account_aliases(), value=ALL, label="Account")
        sort_radio = gr.Radio(SORT_CHOICES, value=db.SORT_DEADLINE, label="Sort")

    pending_ids = gr.State(None)                        # ids waiting for "Delete" to be confirmed

    with gr.Row():
        # Left column: open to-do list, bulk actions, editor
        with gr.Column(scale=3):
            open_list = gr.CheckboxGroup(label="Open to-dos", show_label=True, show_select_all=True)
            with gr.Row():
                done_btn = gr.Button("Mark done", variant="primary")
                delete_btn = gr.Button("Delete", variant="stop")
            with gr.Row(visible=False) as confirm_row:
                confirm_md = gr.Markdown()
                confirm_btn = gr.Button("Confirm delete", variant="stop", scale=0, min_width=120)
                cancel_btn = gr.Button("Cancel", scale=0, min_width=120)

            with gr.Group():
                edit_dd = gr.Dropdown(label="Edit a to-do (open ones)", choices=[], value=None)
                with gr.Row():
                    title_tb = gr.Textbox(label="Title", scale=3)
                    deadline_tb = gr.Textbox(label="Deadline", placeholder="YYYY-MM-DD, empty for none",
                                             scale=2)
                note_tb = gr.Textbox(label="Note", lines=2)
            save_btn = gr.Button("Save")

        # Right column: reminders (counts, action results), source email, done items
        with gr.Column(scale=2):
            status_md = gr.Markdown()
            source_md = gr.Markdown("Select a to-do to edit.")
            with gr.Accordion("Done", open=False):
                done_list = gr.CheckboxGroup(label=f"The {DONE_LIMIT} most recently completed",
                                             show_label=True)
                undo_btn = gr.Button("Undo done")

    # Outputs of refresh-type events, in the same order as refresh_todo()'s return value
    outputs = [open_list, edit_dd, title_tb, deadline_tb, note_tb, source_md,
               done_list, confirm_row, pending_ids, status_md]
    view = [account_dd, sort_radio, edit_dd]          # Inputs needed to refresh: filter, sort, to-do being edited

    demo.load(on_todo_refresh, inputs=view, outputs=outputs)
    account_dd.change(on_todo_refresh, inputs=view, outputs=outputs)
    sort_radio.change(on_todo_refresh, inputs=view, outputs=outputs)

    done_btn.click(on_mark_done, inputs=[open_list] + view, outputs=outputs)
    delete_btn.click(on_ask_delete, inputs=[open_list, account_dd],
                     outputs=[confirm_row, confirm_md, pending_ids, status_md])
    delete_event = confirm_btn.click(on_confirm_delete, inputs=[pending_ids] + view,
                                     outputs=outputs)
    cancel_btn.click(on_cancel_delete, inputs=None, outputs=[confirm_row, pending_ids])

    # Respond only to manual selection; on refresh, refresh_todo() already handles the editor
    edit_dd.input(on_select_todo, inputs=edit_dd,
                  outputs=[title_tb, deadline_tb, note_tb, source_md])
    save_btn.click(on_save_todo,
                   inputs=[edit_dd, title_tb, deadline_tb, note_tb, account_dd, sort_radio],
                   outputs=outputs)
    undo_btn.click(on_undo_done, inputs=[done_list] + view, outputs=outputs)

    return {"delete": delete_event,
            "refresh": {"fn": on_todo_refresh, "inputs": view, "outputs": outputs}}


def build_discarded_tab(demo):
    """Build the contents of the "Discarded" tab in place and bind its events.

    Returns {"restore": the "Restore to review" button event, "refresh": the event arguments that
    refresh this tab (fn / inputs / outputs)}, so build_app can refresh the Triage tab after a
    restore and refresh this tab after discarding on the Triage tab.
    """
    status_md = gr.Markdown()
    discarded_list = gr.CheckboxGroup(label=f"The {DISCARDED_LIMIT} most recently discarded",
                                      show_label=True, show_select_all=True)
    restore_btn = gr.Button("Restore to review", variant="primary")

    # Outputs of refresh-type events, in the same order as refresh_discarded()'s return value
    outputs = [discarded_list, status_md]

    demo.load(on_discarded_refresh, inputs=None, outputs=outputs)
    restore_event = restore_btn.click(on_restore, inputs=discarded_list, outputs=outputs)

    return {"restore": restore_event,
            "refresh": {"fn": on_discarded_refresh, "inputs": None, "outputs": outputs}}


def build_sort_tab(demo):
    """Build the contents of the "Triage" tab in place and bind its events.

    Returns {"add": the "Add to to-dos" event, "discards": ["Discard ticked" and "Confirm discard" events],
             "refresh": the event arguments that refresh this tab (fn / inputs / outputs)},
    so build_app can use .then to refresh the To-dos and Discarded tabs, and refresh this tab
    after a restore on the Discarded tab.
    """
    with gr.Row():
        pull_btn = gr.Button("Fetch and analyze", variant="primary", scale=0, min_width=160)
        status_md = gr.Markdown()
    with gr.Row():
        account_dd = gr.Dropdown([ALL] + account_aliases(), value=ALL, label="Account")
        priority_dd = gr.Dropdown(PRIORITY_CHOICES, value=ALL, label="Priority")

    mail_list = gr.CheckboxGroup(label="Emails to review", show_label=True, show_select_all=True)
    with gr.Row():
        add_btn = gr.Button("Add to to-dos", variant="primary")
        discard_btn = gr.Button("Discard ticked")
        discard_rest_btn = gr.Button("Discard all the rest", variant="stop")

    with gr.Row(visible=False) as confirm_row:
        confirm_md = gr.Markdown()
        confirm_btn = gr.Button("Confirm discard", variant="stop", scale=0, min_width=120)
        cancel_btn = gr.Button("Cancel", scale=0, min_width=120)
    pending_keys = gr.State(None)                       # Keys waiting for "Discard all the rest" to be confirmed

    detail_dd = gr.Dropdown(label="View details (emails to review)", choices=[], value=None)
    detail_md = gr.Markdown("Select an email to view its details.")

    # Outputs of refresh-type events, in the same order as refresh()'s return value
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

    # Respond only to manual selection; on refresh, refresh() already updates the details directly
    detail_dd.input(on_select_detail, inputs=detail_dd, outputs=detail_md)

    return {"add": add_event,
            "discards": [discard_event, confirm_event],
            "refresh": {"fn": on_refresh, "inputs": filters + [detail_dd], "outputs": outputs}}


def build_app():
    """Build the whole UI: the Triage, To-dos and Discarded tabs."""
    with gr.Blocks(title="Mail Triage") as demo:
        with gr.Tabs():
            with gr.Tab("Triage"):
                sort_tab = build_sort_tab(demo)
            with gr.Tab("To-dos"):
                todo_tab = build_todo_tab(demo)
            with gr.Tab("Discarded"):
                discarded_tab = build_discarded_tab(demo)
        # Each refresh below runs after the previous event finishes (whether or not it succeeded),
        # so switching to that tab shows the result right away
        sort_tab["add"].then(**todo_tab["refresh"])            # Add to to-dos → refresh the To-dos tab
        for event in sort_tab["discards"]:                     # Discard ticked / confirm discard → refresh the Discarded tab
            event.then(**discarded_tab["refresh"])
        todo_tab["delete"].then(**discarded_tab["refresh"])    # Delete to-dos → refresh the Discarded tab
        discarded_tab["restore"].then(**sort_tab["refresh"])   # Restore to review → refresh the Triage tab
    return demo


def main():
    db.init_db()
    # Listen on this machine only; share=False is written out explicitly so GRADIO_SHARE=True
    # in the environment cannot create a public link
    build_app().launch(server_name="127.0.0.1", share=False)


if __name__ == "__main__":
    main()
