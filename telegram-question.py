#!/usr/bin/env python3
"""
Telegram-based question answering for Claude Code.
Intercepts AskUserQuestion via PreToolUse hook.

Communicates with telegram-listener.py via file-based IPC:
  ~/.claude/telegram-pending/   — write request here
  ~/.claude/telegram-responses/ — poll for answer here

The listener is the sole Telegram API consumer. This script never
calls getUpdates or sendMessage directly.
"""

import sys
import json
import time
import os
import uuid
import fcntl
import html as _html

TIMEOUT = 180  # 3 minutes for questions (more thinking time)
STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
PENDING_DIR = os.path.expanduser("~/.claude/telegram-pending")
RESPONSE_DIR = os.path.expanduser("~/.claude/telegram-responses")


# ── HTML helpers ──

def esc(s):
    return _html.escape(str(s if s is not None else ""))


def code(s):
    return f"<code>{esc(s)}</code>"


def b(s):
    return f"<b>{esc(s)}</b>"


# ── State ──

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"default": "on", "projects": {}, "active": {}}
    try:
        with open(STATE_FILE) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            state = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        state.setdefault("default", "on")
        state.setdefault("projects", {})
        state.setdefault("active", {})
        return state
    except (json.JSONDecodeError, IOError):
        return {"default": "on", "projects": {}, "active": {}}


def get_mode_for_project(project):
    state = load_state()
    # Global "off" is a master kill switch — overrides all per-project settings
    if state["default"] == "off":
        return "off"
    key = project.lower()
    if key in state["projects"]:
        return state["projects"][key]
    return state["default"]


# ── IPC helpers ──

def listener_alive():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def write_request(data):
    os.makedirs(PENDING_DIR, exist_ok=True)
    path = os.path.join(PENDING_DIR, f"{data['id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def poll_response(req_uuid, timeout):
    path = os.path.join(RESPONSE_DIR, f"{req_uuid}.json")
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    response = json.load(f)
                os.remove(path)
                return response
            except (json.JSONDecodeError, IOError):
                time.sleep(0.2)
                continue
        time.sleep(0.5)
    return None


# ── Question formatting ──

def format_question(project, question_data):
    q = question_data.get("question", "No question text")
    header = question_data.get("header", "")
    multi = question_data.get("multiSelect", False)
    options = question_data.get("options", [])

    msg = f"❓ <b>Question from</b> {code(project)}\n\n"
    if header:
        msg += f"{b(header)}\n"
    msg += f"{esc(q)}\n\n"

    for idx, opt in enumerate(options):
        label = opt.get("label", f"Option {idx+1}")
        desc = opt.get("description", "")
        msg += f"<b>{idx+1}.</b> {esc(label)}"
        if desc:
            msg += f"\n   <i>{esc(desc)}</i>"
        msg += "\n"

    if multi:
        msg += "\n<i>Multi-select: you can pick several.</i>"

    return msg


def build_keyboard(options, request_id):
    buttons = []
    for idx, opt in enumerate(options):
        label = opt.get("label", f"Option {idx+1}")
        buttons.append([{
            "text": f"{idx+1}. {label}",
            "callback_data": f"q:{request_id}:{idx}",
        }])
    buttons.append([{
        "text": "💬 Other (type answer)",
        "callback_data": f"q:{request_id}:other",
    }])
    return {"inline_keyboard": buttons}


def build_multi_keyboard(options, request_id):
    buttons = []
    for idx, opt in enumerate(options):
        label = opt.get("label", f"Option {idx+1}")
        buttons.append([{
            "text": f"⬜ {label}",
            "callback_data": f"q:{request_id}:{idx}",
        }])
    buttons.append([{
        "text": "✅ Done — Submit",
        "callback_data": f"q:{request_id}:done",
    }])
    buttons.append([{
        "text": "💬 Other (type answer)",
        "callback_data": f"q:{request_id}:other",
    }])
    return {"inline_keyboard": buttons}


# ── Main ──

def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    project = os.path.basename(hook_input.get("cwd", ""))
    mode = get_mode_for_project(project)

    if mode == "off":
        sys.exit(0)

    if not listener_alive():
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    questions = tool_input.get("questions", [])
    if not questions:
        sys.exit(0)

    answers = {}

    for q_data in questions:
        question_text = q_data.get("question", "")
        options = q_data.get("options", [])
        multi = q_data.get("multiSelect", False)

        request_id = uuid.uuid4().hex[:8]
        req_uuid = uuid.uuid4().hex
        message = format_question(project, q_data)

        if options:
            if multi:
                keyboard = build_multi_keyboard(options, request_id)
            else:
                keyboard = build_keyboard(options, request_id)
        else:
            keyboard = None
            message += "\n<i>Type your answer below:</i>"

        write_request({
            "type": "question",
            "id": req_uuid,
            "project": project,
            "message_html": message,
            "keyboard": keyboard,
            "request_id": request_id,
            "options": options,
            "multi_select": multi,
            "timeout": TIMEOUT,
            "created_at": int(time.time()),
        })

        response = poll_response(req_uuid, TIMEOUT)

        if not response or response.get("answer") in (None, "timeout", "cancelled", "error"):
            sys.exit(0)  # fall through to terminal

        answers[question_text] = response["answer"]

    # Feed answers back to Claude via stderr + exit 2 (block tool).
    if len(answers) == 1:
        feedback = f"User answered: {list(answers.values())[0]}"
    else:
        lines = []
        for q, a in answers.items():
            short_q = q[:80] + "..." if len(q) > 80 else q
            lines.append(f'Q: "{short_q}" → User answered: {a}')
        feedback = "\n".join(lines)

    print(feedback, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
