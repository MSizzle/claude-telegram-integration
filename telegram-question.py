#!/usr/bin/env python3
"""
Telegram-based question answering for Claude Code.
Intercepts AskUserQuestion via PreToolUse hook.
Sends options as inline buttons, waits for response,
then blocks the tool with the user's answer as feedback.

Respects the same on/off/auto state as telegram-approve.
In auto mode, questions always escalate to interactive (can't auto-answer).
"""

import sys
import json
import time
import urllib.request
import os
import uuid
import fcntl
import signal
import html as _html

POLL_INTERVAL = 1.5
TIMEOUT = 180  # 3 minutes for questions (more thinking time)
STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
CONFIG_FILE = os.path.expanduser("~/.claude/telegram-config.json")
LISTENER_PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
POLL_LOCK_FILE = os.path.expanduser("~/.claude/telegram-poll.lock")
PAUSE_TIMESTAMP_FILE = os.path.expanduser("~/.claude/telegram-listener-paused-at")


# ── Credentials ──

def load_credentials():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            token = token or cfg.get("token")
            chat_id = chat_id or str(cfg.get("chat_id", ""))
        except (FileNotFoundError, json.JSONDecodeError, IOError):
            pass
    if not (token and chat_id):
        print(
            "Telegram credentials missing. Set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
            f"or create {CONFIG_FILE}.",
            file=sys.stderr,
        )
        sys.exit(0)
    return token, chat_id


TOKEN, CHAT_ID = load_credentials()
API = f"https://api.telegram.org/bot{TOKEN}"


# ── HTML helpers ──

def esc(s):
    return _html.escape(str(s if s is not None else ""))


def code(s):
    return f"<code>{esc(s)}</code>"


def b(s):
    return f"<b>{esc(s)}</b>"


# ── Listener / lock coordination ──

def pause_listener():
    if os.path.exists(LISTENER_PID_FILE):
        try:
            with open(LISTENER_PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGSTOP)
            with open(PAUSE_TIMESTAMP_FILE, "w") as f:
                f.write(str(int(time.time())))
            return pid
        except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
            pass
    return None


def resume_listener(pid):
    if pid:
        try:
            os.kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.remove(PAUSE_TIMESTAMP_FILE)
    except FileNotFoundError:
        pass


def rescue_stale_listener():
    """Resume the listener if it's been paused for too long (>60s)."""
    if not os.path.exists(PAUSE_TIMESTAMP_FILE):
        return
    try:
        with open(PAUSE_TIMESTAMP_FILE) as f:
            paused_at = int(f.read().strip())
        if int(time.time()) - paused_at > 60:
            if os.path.exists(LISTENER_PID_FILE):
                with open(LISTENER_PID_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGCONT)
            os.remove(PAUSE_TIMESTAMP_FILE)
    except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
        try:
            os.remove(PAUSE_TIMESTAMP_FILE)
        except FileNotFoundError:
            pass


class PollSession:
    """Serialize getUpdates and pause listener; guarantees cleanup on all exits."""

    def __init__(self):
        self.lock_fh = None
        self.listener_pid = None

    def __enter__(self):
        rescue_stale_listener()
        try:
            self.lock_fh = open(POLL_LOCK_FILE, "w")
            fcntl.flock(self.lock_fh, fcntl.LOCK_EX)
        except (IOError, OSError):
            self.lock_fh = None
        self.listener_pid = pause_listener()
        return self

    def __exit__(self, exc_type, exc, tb):
        resume_listener(self.listener_pid)
        if self.lock_fh:
            try:
                fcntl.flock(self.lock_fh, fcntl.LOCK_UN)
                self.lock_fh.close()
            except (IOError, OSError):
                pass
        return False


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
    key = project.lower()
    if key in state["projects"]:
        return state["projects"][key]
    return state["default"]


# ── Telegram ──

def telegram_request(method, data):
    url = f"{API}/{method}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"Telegram API error: {e}", file=sys.stderr)
        return None


def send_message(text, reply_markup=None):
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    result = telegram_request("sendMessage", data)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def is_allowed_sender(from_obj):
    if not from_obj:
        return False
    return str(from_obj.get("id", "")) == str(CHAT_ID)


# ── Project picker callback handling (shared UX with listener) ──

MODE_LABELS = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}
VALID_MODES = ("on", "off", "auto")


def save_state(state):
    now = int(time.time())
    state["active"] = {
        k: v for k, v in state.get("active", {}).items()
        if now - v.get("last_seen", 0) < 1800
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, STATE_FILE)


def collect_projects(state):
    now = int(time.time())
    seen = {}
    for k, info in state.get("active", {}).items():
        if now - info.get("last_seen", 0) < 1800:
            seen[k] = info.get("name", k)
    for k in state.get("projects", {}):
        seen.setdefault(k, k)
    return seen


def format_projects_text(state):
    projects = collect_projects(state)
    if not projects:
        return (
            "📋 <b>Projects</b>\n\n<i>No active or overridden projects.</i>\n"
            f"<b>Global default:</b> {esc(MODE_LABELS.get(state['default'], state['default']))}"
        )
    lines = ["📋 <b>Projects</b> — tap to set mode\n"]
    for key in sorted(projects, key=lambda k: projects[k].lower()):
        name = projects[key]
        if key in state["projects"]:
            mode = state["projects"][key]
            suffix = f"{esc(MODE_LABELS.get(mode, mode))} <i>(override)</i>"
        else:
            mode = state["default"]
            suffix = f"{esc(MODE_LABELS.get(mode, mode))} <i>(global)</i>"
        lines.append(f"• {b(name)} — {suffix}")
    lines.append(f"\n<b>Global default:</b> {esc(MODE_LABELS.get(state['default'], state['default']))}")
    return "\n".join(lines)


def build_projects_keyboard(state):
    projects = collect_projects(state)
    rows = []
    for key in sorted(projects, key=lambda k: projects[k].lower()):
        name = projects[key]
        short = name if len(name) <= 22 else name[:20] + "…"
        # Telegram callback_data is limited to 64 bytes; truncate key to stay safe
        cb_key = key[:50]
        rows.append([{"text": f"— {short} —", "callback_data": f"pn:{cb_key}"}])
        rows.append([
            {"text": "✅ on", "callback_data": f"pm:{cb_key}:on"},
            {"text": "🔇 off", "callback_data": f"pm:{cb_key}:off"},
            {"text": "🚀 auto", "callback_data": f"pm:{cb_key}:auto"},
            {"text": "🧹 clear", "callback_data": f"pm:{cb_key}:clear"},
        ])
    rows.append([{"text": "🔄 Refresh", "callback_data": "pr:refresh"}])
    return {"inline_keyboard": rows}


def handle_project_callback(cb):
    data = cb.get("data", "")
    if not (data.startswith("pm:") or data.startswith("pn:") or data == "pr:refresh"):
        return False

    cb_id = cb.get("id")
    msg = cb.get("message", {}) or {}
    picker_msg_id = msg.get("message_id")

    if data.startswith("pm:"):
        try:
            _, key, mode = data.split(":", 2)
        except ValueError:
            telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return True

        state = load_state()
        if mode == "clear":
            if key in state.get("projects", {}):
                del state["projects"][key]
                save_state(state)
                toast = "Override cleared"
            else:
                toast = "No override to clear"
        elif mode in VALID_MODES:
            state.setdefault("projects", {})[key] = mode
            save_state(state)
            toast = MODE_LABELS.get(mode, mode)
        else:
            telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return True

        telegram_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": toast})
        if picker_msg_id:
            fresh = load_state()
            telegram_request("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": picker_msg_id,
                "text": format_projects_text(fresh),
                "parse_mode": "HTML",
                "reply_markup": build_projects_keyboard(fresh),
            })
        return True

    if data.startswith("pn:"):
        key = data[3:]
        state = load_state()
        if key in state.get("projects", {}):
            mode = state["projects"][key]
            src = "override"
        else:
            mode = state["default"]
            src = "global"
        telegram_request("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": f"{MODE_LABELS.get(mode, mode)} ({src})",
        })
        return True

    if data == "pr:refresh":
        if picker_msg_id:
            fresh = load_state()
            telegram_request("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": picker_msg_id,
                "text": format_projects_text(fresh),
                "parse_mode": "HTML",
                "reply_markup": build_projects_keyboard(fresh),
            })
        telegram_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Refreshed"})
        return True

    return False


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


def build_multi_keyboard(options, selected, request_id):
    buttons = []
    for idx, opt in enumerate(options):
        label = opt.get("label", f"Option {idx+1}")
        check = "✅" if idx in selected else "⬜"
        buttons.append([{
            "text": f"{check} {label}",
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


def handle_command(text):
    """Process mode-change commands that arrive during question polling.
    Returns the mode set ('on'/'off'/'auto'/'clear') for mode changes,
    True for other handled commands, False if not a command."""
    lower = text.strip().lower()
    labels = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}

    if lower in ("/on", "/off", "/auto"):
        mode = lower[1:]
        state = load_state()
        state["default"] = mode
        save_state(state)
        send_message(f"✅ Global → {esc(labels[mode])}")
        return mode

    if text.startswith("@") and " " in text:
        parts = text[1:].split(None, 1)
        project_name = parts[0].lower()
        cmd = parts[1].lower().strip()
        state = load_state()
        if cmd in VALID_MODES:
            state.setdefault("projects", {})[project_name] = cmd
            save_state(state)
            send_message(f"🎯 {code(parts[0])} → {esc(labels[cmd])}")
            return cmd
        elif cmd == "clear":
            if project_name in state.get("projects", {}):
                del state["projects"][project_name]
                save_state(state)
            send_message(f"🔄 {code(parts[0])} — override cleared")
            return "clear"

    return False


def poll_for_text(msg_id, project):
    """Poll for a free-text response (no inline buttons)."""
    start = time.time()
    last_update_id = 0

    def _check_off_after_command(cmd_result):
        if not cmd_result:
            return False
        new_mode = get_mode_for_project(project)
        if new_mode == "off":
            telegram_request("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": msg_id,
                "text": "🔇 <b>Cancelled</b> — approvals turned OFF",
                "parse_mode": "HTML",
            })
            return True
        return False

    # Drain pending updates — process commands.
    drain = telegram_request("getUpdates", {"offset": 0, "limit": 100, "timeout": 0})
    if drain and drain.get("ok"):
        for update in drain.get("result", []):
            last_update_id = update["update_id"] + 1
            msg = update.get("message")
            if msg and msg.get("text", ""):
                if str(msg.get("chat", {}).get("id", "")) == str(CHAT_ID) and is_allowed_sender(msg.get("from")):
                    result = handle_command(msg["text"])
                    if _check_off_after_command(result):
                        return None
            cb = update.get("callback_query")
            if cb and is_allowed_sender(cb.get("from")):
                if handle_project_callback(cb):
                    if _check_off_after_command(True):
                        return None

    while time.time() - start < TIMEOUT:
        result = telegram_request("getUpdates", {
            "offset": last_update_id,
            "limit": 10,
            "timeout": 2,
            "allowed_updates": ["callback_query", "message"],
        })

        if not result or not result.get("ok"):
            time.sleep(POLL_INTERVAL)
            continue

        for update in result.get("result", []):
            last_update_id = update["update_id"] + 1

            msg = update.get("message")
            if msg and msg.get("text", ""):
                if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                    continue
                if not is_allowed_sender(msg.get("from")):
                    continue

                text = msg["text"].strip()
                # Process mode-change commands
                if text.startswith("/") or text.startswith("@"):
                    cmd_result = handle_command(text)
                    if _check_off_after_command(cmd_result):
                        return None
                    continue

                # Any other text is the user's answer
                telegram_request("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": f"💬 <b>Answered:</b> {esc(text)}",
                    "parse_mode": "HTML",
                })
                return text

            cb = update.get("callback_query")
            if cb and is_allowed_sender(cb.get("from")):
                if handle_project_callback(cb):
                    if _check_off_after_command(True):
                        return None
                else:
                    telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb.get("id"),
                    })

        time.sleep(POLL_INTERVAL)

    telegram_request("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": "⏰ <b>Timed out</b> — no answer received, falling back to terminal",
        "parse_mode": "HTML",
    })
    return None


def poll_for_answer(request_id, msg_id, options, project, multi_select=False):
    start = time.time()
    last_update_id = 0
    selected = set()
    waiting_for_text = False

    def _check_off_after_command(cmd_result):
        """After a command, check if this project is now off."""
        if not cmd_result:
            return False
        new_mode = get_mode_for_project(project)
        if new_mode == "off":
            telegram_request("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": msg_id,
                "text": "🔇 <b>Cancelled</b> — approvals turned OFF",
                "parse_mode": "HTML",
            })
            return True
        return False

    # Drain pending updates WITHOUT discarding them — process commands.
    drain = telegram_request("getUpdates", {"offset": 0, "limit": 100, "timeout": 0})
    if drain and drain.get("ok"):
        for update in drain.get("result", []):
            last_update_id = update["update_id"] + 1
            msg = update.get("message")
            if msg and msg.get("text", ""):
                if str(msg.get("chat", {}).get("id", "")) == str(CHAT_ID) and is_allowed_sender(msg.get("from")):
                    result = handle_command(msg["text"])
                    if _check_off_after_command(result):
                        return None
            cb = update.get("callback_query")
            if cb and is_allowed_sender(cb.get("from")):
                if handle_project_callback(cb):
                    if _check_off_after_command(True):
                        return None

    while time.time() - start < TIMEOUT:
        result = telegram_request("getUpdates", {
            "offset": last_update_id,
            "limit": 10,
            "timeout": 2,
            "allowed_updates": ["callback_query", "message"],
        })

        if not result or not result.get("ok"):
            time.sleep(POLL_INTERVAL)
            continue

        for update in result.get("result", []):
            last_update_id = update["update_id"] + 1

            msg = update.get("message")
            if msg and msg.get("text", ""):
                # Verify sender
                if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                    continue
                if not is_allowed_sender(msg.get("from")):
                    continue

                text = msg["text"].strip()
                # Always process mode-change commands, even mid-question
                if text.startswith("/") or text.startswith("@"):
                    cmd_result = handle_command(text)
                    if _check_off_after_command(cmd_result):
                        return None
                    continue
                if waiting_for_text:
                    telegram_request("editMessageText", {
                        "chat_id": CHAT_ID,
                        "message_id": msg_id,
                        "text": f"💬 <b>Answered:</b> {esc(text)}",
                        "parse_mode": "HTML",
                    })
                    return text
                continue

            cb = update.get("callback_query")
            if not cb:
                continue

            if not is_allowed_sender(cb.get("from")):
                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb.get("id"),
                    "text": "Unauthorized",
                })
                continue

            data = cb.get("data", "")
            cb_id = cb.get("id")

            # Project-picker buttons — handle inline so they work during a pending question
            if handle_project_callback(cb):
                if _check_off_after_command(True):
                    return None
                continue

            if not data.startswith(f"q:{request_id}:"):
                telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
                continue

            choice = data.split(":")[-1]

            if choice == "other":
                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "Type your answer below...",
                })
                telegram_request("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": "💬 <b>Type your answer:</b>",
                    "parse_mode": "HTML",
                })
                waiting_for_text = True
                continue

            if multi_select:
                if choice == "done":
                    if not selected:
                        telegram_request("answerCallbackQuery", {
                            "callback_query_id": cb_id,
                            "text": "Select at least one option first",
                        })
                        continue

                    labels_list = [options[i]["label"] for i in sorted(selected)]
                    answer = ", ".join(labels_list)

                    telegram_request("answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": f"Submitted: {answer[:100]}",
                    })
                    telegram_request("editMessageText", {
                        "chat_id": CHAT_ID,
                        "message_id": msg_id,
                        "text": f"✅ <b>Selected:</b> {esc(answer)}",
                        "parse_mode": "HTML",
                    })
                    return answer

                try:
                    idx = int(choice)
                except (ValueError, TypeError):
                    telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
                    continue
                if idx < 0 or idx >= len(options):
                    telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
                    continue

                if idx in selected:
                    selected.discard(idx)
                else:
                    selected.add(idx)

                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": f"{'Selected' if idx in selected else 'Deselected'}: {options[idx]['label'][:30]}",
                })

                new_keyboard = build_multi_keyboard(options, selected, request_id)
                telegram_request("editMessageReplyMarkup", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "reply_markup": new_keyboard,
                })
                continue
            else:
                try:
                    idx = int(choice)
                except (ValueError, TypeError):
                    telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
                    continue
                if idx < 0 or idx >= len(options):
                    telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
                    continue

                label = options[idx]["label"]

                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": f"Selected: {label[:30]}",
                })
                telegram_request("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": f"✅ <b>Selected:</b> {esc(label)}",
                    "parse_mode": "HTML",
                })
                return label

        time.sleep(POLL_INTERVAL)

    telegram_request("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": "⏰ <b>Timed out</b> — no answer received, falling back to terminal",
        "parse_mode": "HTML",
    })
    return None


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    project = os.path.basename(hook_input.get("cwd", ""))
    mode = get_mode_for_project(project)

    # OFF — let it show in terminal
    if mode == "off":
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    questions = tool_input.get("questions", [])
    if not questions:
        sys.exit(0)

    answers = {}

    with PollSession():
        for q_data in questions:
            question_text = q_data.get("question", "")
            options = q_data.get("options", [])
            multi = q_data.get("multiSelect", False)

            request_id = uuid.uuid4().hex[:8]
            message = format_question(project, q_data)

            if options:
                if multi:
                    keyboard = build_multi_keyboard(options, set(), request_id)
                else:
                    keyboard = build_keyboard(options, request_id)
            else:
                # Free-text question — no predefined options, just a text prompt
                keyboard = None
                message += "\n<i>Type your answer below:</i>"

            msg_id = send_message(message, reply_markup=keyboard)
            if not msg_id:
                # Couldn't send — bail and let terminal handle it.
                sys.exit(0)

            if options:
                answer = poll_for_answer(request_id, msg_id, options, project, multi_select=multi)
            else:
                answer = poll_for_text(msg_id, project)

            if answer is None:
                # Timed out — fall through to terminal.
                sys.exit(0)

            answers[question_text] = answer

    # Feed the answers back to Claude via stderr + exit 2 (tool block).
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
