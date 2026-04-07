#!/usr/bin/env python3
"""
Telegram listener for Claude Code — sole Telegram API consumer.

Hook scripts (telegram-approve.py, telegram-question.py) communicate via
file-based IPC instead of calling Telegram directly:
  ~/.claude/telegram-pending/   — requests from hooks
  ~/.claude/telegram-responses/ — responses back to hooks

Commands:
  /on          — global interactive mode
  /off         — global disabled
  /auto        — global auto-approve
  @Project on  — per-project override (on/off/auto/clear)
  /projects    — tap-to-set mode per project
  /status      — show all modes
  /quo         — show active sessions and their modes
  /stop        — stop this listener
  /help        — command list

Usage: python3 telegram-listener.py &
"""

import json
import time
import urllib.request
import os
import signal
import sys
import fcntl
import html as _html
import glob as _glob

STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
CONFIG_FILE = os.path.expanduser("~/.claude/telegram-config.json")
PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
PENDING_DIR = os.path.expanduser("~/.claude/telegram-pending")
RESPONSE_DIR = os.path.expanduser("~/.claude/telegram-responses")
VALID_MODES = ("on", "off", "auto")

# In-memory tracking of requests awaiting user response.
# Maps request_id → {uuid, msg_id, type, project, options, multi_select,
#                     selected, waiting_for_text, timeout, created_at}
active_requests = {}


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
        sys.exit(1)
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


# ── Lifecycle ──

def cleanup(signum=None, frame=None):
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)


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


# ── Telegram API ──

def telegram_request(method, data):
    url = f"{API}/{method}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def send_message(text, reply_markup=None):
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    result = telegram_request("sendMessage", data)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def edit_message(msg_id, text):
    telegram_request("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    })


def edit_keyboard(msg_id, reply_markup):
    telegram_request("editMessageReplyMarkup", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "reply_markup": reply_markup,
    })


def answer_cb(cb_id, text=None):
    data = {"callback_query_id": cb_id}
    if text:
        data["text"] = text
    telegram_request("answerCallbackQuery", data)


def is_allowed_sender(from_obj):
    if not from_obj:
        return False
    return str(from_obj.get("id", "")) == str(CHAT_ID)


# ── IPC: File-based request/response ──

def ensure_dirs():
    os.makedirs(PENDING_DIR, exist_ok=True)
    os.makedirs(RESPONSE_DIR, exist_ok=True)


def cleanup_stale_files():
    """Remove request/response files older than 5 minutes on startup."""
    now = time.time()
    for d in (PENDING_DIR, RESPONSE_DIR):
        for path in _glob.glob(os.path.join(d, "*.json")):
            try:
                if now - os.path.getmtime(path) > 300:
                    os.remove(path)
            except OSError:
                pass


def write_response(req_uuid, data):
    path = os.path.join(RESPONSE_DIR, f"{req_uuid}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def scan_pending_requests():
    """Pick up new request files from hooks and send Telegram messages."""
    paths = sorted(_glob.glob(os.path.join(PENDING_DIR, "*.json")))
    for path in paths:
        # Skip .tmp files (partial writes)
        if path.endswith(".tmp"):
            continue
        try:
            with open(path) as f:
                req = json.load(f)
            os.remove(path)
        except (json.JSONDecodeError, IOError, OSError):
            continue

        req_type = req.get("type")
        req_uuid = req.get("id")
        request_id = req.get("request_id", "")

        # Fire-and-forget notification (auto-approve summaries)
        if req_type == "notify":
            send_message(req.get("message_html", ""))
            continue

        # Send message with keyboard
        msg_id = send_message(
            req.get("message_html", ""),
            reply_markup=req.get("keyboard"),
        )
        if not msg_id:
            # Couldn't send — tell hook to fall through to terminal
            if req_uuid:
                write_response(req_uuid, {
                    "answer": "error",
                    "responded_at": int(time.time()),
                })
            continue

        # Track this request
        active_requests[request_id] = {
            "uuid": req_uuid,
            "msg_id": msg_id,
            "type": req_type,
            "project": req.get("project", ""),
            "options": req.get("options", []),
            "multi_select": req.get("multi_select", False),
            "selected": set(),
            "waiting_for_text": False,
            "timeout": req.get("timeout", 120),
            "created_at": req.get("created_at", time.time()),
        }


def expire_stale_requests():
    """Time out requests that have been pending too long."""
    now = time.time()
    to_expire = [
        rid for rid, req in active_requests.items()
        if now - req["created_at"] > req["timeout"]
    ]
    for rid in to_expire:
        req = active_requests.pop(rid)
        edit_message(req["msg_id"],
                     "⏰ <b>Timed out</b> — no response received")
        write_response(req["uuid"], {
            "answer": "timeout",
            "responded_at": int(now),
        })


def cancel_all_requests():
    """Cancel every active request (used when global mode → off)."""
    for rid in list(active_requests.keys()):
        req = active_requests.pop(rid)
        edit_message(req["msg_id"],
                     "🔇 <b>Cancelled</b> — approvals turned OFF")
        write_response(req["uuid"], {
            "answer": "cancelled",
            "responded_at": int(time.time()),
        })


def cancel_requests_for_project(project_key):
    """Cancel active requests for a specific project."""
    to_cancel = [
        rid for rid, req in active_requests.items()
        if req.get("project", "").lower() == project_key
    ]
    for rid in to_cancel:
        req = active_requests.pop(rid)
        edit_message(req["msg_id"],
                     "🔇 <b>Cancelled</b> — approvals turned OFF")
        write_response(req["uuid"], {
            "answer": "cancelled",
            "responded_at": int(time.time()),
        })


# ── Question keyboard builders ──

def build_question_keyboard(options, request_id):
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


# ── Project picker keyboard ──

MODE_ICONS = {"on": "✅", "off": "🔇", "auto": "🚀"}
MODE_LABELS = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}


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
            "📋 <b>Projects</b>\n\n"
            "<i>No active or overridden projects.</i>\n"
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


def refresh_projects_message(msg_id):
    state = load_state()
    telegram_request("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": format_projects_text(state),
        "parse_mode": "HTML",
        "reply_markup": build_projects_keyboard(state),
    })


def handle_project_callback(cb):
    """Handle project-picker button presses. Returns True if handled."""
    data = cb.get("data", "")
    if not (data.startswith("pm:") or data.startswith("pn:") or data == "pr:refresh"):
        return False

    cb_id = cb.get("id")
    msg = cb.get("message", {}) or {}
    msg_id = msg.get("message_id")

    if data.startswith("pm:"):
        try:
            _, key, mode = data.split(":", 2)
        except ValueError:
            answer_cb(cb_id)
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
            # Cancel requests if project was set to off
            if mode == "off":
                cancel_requests_for_project(key)
        else:
            answer_cb(cb_id)
            return True

        answer_cb(cb_id, toast)
        if msg_id:
            refresh_projects_message(msg_id)
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
        answer_cb(cb_id, f"{MODE_LABELS.get(mode, mode)} ({src})")
        return True

    if data == "pr:refresh":
        if msg_id:
            refresh_projects_message(msg_id)
        answer_cb(cb_id, "Refreshed")
        return True

    return False


# ── Callback routing ──

def handle_callback(cb):
    """Route a callback query to the right handler."""
    if not is_allowed_sender(cb.get("from")):
        answer_cb(cb.get("id"), "Unauthorized")
        return

    data = cb.get("data", "")
    cb_id = cb.get("id")

    # Project picker buttons
    if handle_project_callback(cb):
        return

    # Approve / Deny
    if data.startswith("approve:") or data.startswith("deny:"):
        parts = data.split(":", 1)
        action = parts[0]
        request_id = parts[1] if len(parts) > 1 else ""

        if request_id in active_requests:
            req = active_requests.pop(request_id)
            answer_cb(cb_id,
                       "Approved ✅" if action == "approve" else "Denied ❌")
            status = "✅ <b>Approved</b>" if action == "approve" else "❌ <b>Denied</b>"
            elapsed = int(time.time() - req["created_at"])
            edit_message(req["msg_id"],
                         f"{status}\n\n(responded in {elapsed}s)")
            write_response(req["uuid"], {
                "answer": action,
                "responded_at": int(time.time()),
            })
        else:
            answer_cb(cb_id)
        return

    # Question callbacks: q:{request_id}:{choice}
    if data.startswith("q:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            request_id = parts[1]
            choice = parts[2]
            if request_id in active_requests:
                handle_question_callback(request_id, choice, cb_id)
                return
        answer_cb(cb_id)
        return

    answer_cb(cb_id)


def handle_question_callback(request_id, choice, cb_id):
    """Handle a button press on a question message."""
    req = active_requests[request_id]
    options = req["options"]

    # "Other" — switch to free-text mode
    if choice == "other":
        answer_cb(cb_id, "Type your answer below...")
        edit_message(req["msg_id"], "💬 <b>Type your answer:</b>")
        req["waiting_for_text"] = True
        return

    # Multi-select mode
    if req["multi_select"]:
        if choice == "done":
            if not req["selected"]:
                answer_cb(cb_id, "Select at least one option first")
                return
            labels = [options[i]["label"] for i in sorted(req["selected"])]
            answer = ", ".join(labels)
            answer_cb(cb_id, f"Submitted: {answer[:100]}")
            edit_message(req["msg_id"],
                         f"✅ <b>Selected:</b> {esc(answer)}")
            write_response(req["uuid"], {
                "answer": answer,
                "responded_at": int(time.time()),
            })
            del active_requests[request_id]
            return

        try:
            idx = int(choice)
        except (ValueError, TypeError):
            answer_cb(cb_id)
            return
        if idx < 0 or idx >= len(options):
            answer_cb(cb_id)
            return

        if idx in req["selected"]:
            req["selected"].discard(idx)
        else:
            req["selected"].add(idx)

        label = options[idx]["label"][:30]
        selected = idx in req["selected"]
        answer_cb(cb_id,
                   f"{'Selected' if selected else 'Deselected'}: {label}")
        edit_keyboard(req["msg_id"],
                      build_multi_keyboard(options, req["selected"],
                                           request_id))
        return

    # Single-select mode
    try:
        idx = int(choice)
    except (ValueError, TypeError):
        answer_cb(cb_id)
        return
    if idx < 0 or idx >= len(options):
        answer_cb(cb_id)
        return

    label = options[idx]["label"]
    answer_cb(cb_id, f"Selected: {label[:30]}")
    edit_message(req["msg_id"], f"✅ <b>Selected:</b> {esc(label)}")
    write_response(req["uuid"], {
        "answer": label,
        "responded_at": int(time.time()),
    })
    del active_requests[request_id]


# ── Command handling ──

def handle_command(text):
    text = text.strip()
    lower = text.lower()
    labels = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}

    if text.startswith("@") and " " in text:
        parts = text[1:].split(None, 1)
        project_name = parts[0].lower()
        cmd = parts[1].lower().strip()

        state = load_state()

        if cmd == "clear":
            if project_name in state["projects"]:
                del state["projects"][project_name]
                save_state(state)
                send_message(
                    f"🔄 {code(parts[0])} — override removed, using global default "
                    f"({b(state['default'])})"
                )
            else:
                send_message(f"{code(parts[0])} has no override — already using global default")
            return True
        elif cmd in VALID_MODES:
            state["projects"][project_name] = cmd
            save_state(state)
            send_message(f"🎯 {code(parts[0])} → {b(labels[cmd])}")
            if cmd == "off":
                cancel_requests_for_project(project_name)
            return True
        else:
            send_message(f"Unknown mode {code(cmd)}. Use: on, off, auto, clear")
            return True

    if lower == "/on":
        state = load_state()
        state["default"] = "on"
        save_state(state)
        send_message("✅ <b>Global: Interactive mode ON</b>\n\nApprove/Deny buttons for all projects (unless overridden).")
        return True
    elif lower == "/off":
        state = load_state()
        state["default"] = "off"
        save_state(state)
        send_message("🔇 <b>Global: Approvals OFF</b>\n\nAll projects use terminal prompts (unless overridden).")
        cancel_all_requests()
        return True
    elif lower == "/auto off":
        state = load_state()
        if state["default"] == "auto":
            state["default"] = "on"
        cleared = [k for k, v in state.get("projects", {}).items() if v == "auto"]
        for k in cleared:
            del state["projects"][k]
        save_state(state)
        msg = "🛑 <b>All auto-approve disabled</b>\n\n"
        msg += f"<b>Global:</b> {esc(labels.get(state['default'], state['default']))}\n"
        if cleared:
            msg += f"<b>Cleared auto from:</b> {esc(', '.join(cleared))}"
        else:
            msg += "No per-project auto overrides to clear."
        send_message(msg)
        return True
    elif lower == "/auto":
        state = load_state()
        state["default"] = "auto"
        save_state(state)
        send_message("🚀 <b>Global: Auto-approve ON</b>\n\nAll projects auto-approved with summaries (unless overridden).")
        return True
    elif lower == "/status":
        state = load_state()
        msg = f"📊 <b>Status</b>\n\n<b>Global:</b> {esc(labels.get(state['default'], state['default']))}\n"
        if state["projects"]:
            msg += "\n<b>Per-project overrides:</b>\n"
            for proj, mode in sorted(state["projects"].items()):
                msg += f"  {code(proj)} → {esc(labels.get(mode, mode))}\n"
        else:
            msg += "\nNo per-project overrides."
        if active_requests:
            msg += f"\n\n<b>Pending requests:</b> {len(active_requests)}"
        send_message(msg)
        return True
    elif lower == "/quo":
        state = load_state()
        now = int(time.time())

        active = {}
        for key, info in state.get("active", {}).items():
            age = now - info.get("last_seen", 0)
            if age < 1800:
                active[key] = info

        if not active:
            send_message("📋 <b>Queue</b>\n\nNo active sessions.")
            return True

        msg = "📋 <b>Active Sessions</b>\n\n"
        for key in sorted(active.keys()):
            info = active[key]
            name = info.get("name", key)
            age = now - info.get("last_seen", 0)
            if key in state["projects"]:
                mode = state["projects"][key]
                source = "override"
            else:
                mode = state["default"]
                source = "global"
            label = labels.get(mode, mode)

            if age < 60:
                age_str = f"{age}s ago"
            else:
                age_str = f"{age // 60}m ago"

            msg += f"• {code(name)} — {esc(label)}"
            if source == "override":
                msg += " (custom)"
            msg += f"\n  <i>Last activity: {esc(age_str)}</i>\n"

        msg += f"\n<b>Global default:</b> {esc(labels.get(state['default'], state['default']))}"
        send_message(msg)
        return True
    elif lower == "/projects":
        state = load_state()
        send_message(
            format_projects_text(state),
            reply_markup=build_projects_keyboard(state),
        )
        return True
    elif lower == "/stop":
        send_message("👋 <b>Listener stopped</b>")
        cleanup()
        return True
    elif lower == "/help":
        send_message(
            "🤖 <b>Commands</b>\n\n"
            "/on — global interactive mode\n"
            "/off — global disabled\n"
            "/auto — global auto-approve\n"
            "/projects — tap-to-set mode per project\n"
            "@Project on — per-project (on/off/auto/clear)\n"
            "/status — show all modes\n"
            "/quo — active sessions queue\n"
            "/stop — stop listener"
        )
        return True

    return False


# ── Main loop ──

def main():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    ensure_dirs()
    cleanup_stale_files()

    # Migrate old state file if it exists
    old_state = os.path.expanduser("~/.claude/telegram-approve.state")
    if os.path.exists(old_state) and not os.path.exists(STATE_FILE):
        with open(old_state) as f:
            old_mode = f.read().strip()
        if old_mode in VALID_MODES:
            save_state({"default": old_mode, "projects": {}, "active": {}})
        os.remove(old_state)

    labels = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}
    state = load_state()
    mode = state["default"]
    send_message(
        f"🤖 <b>Telegram listener started</b>\n\n"
        f"<b>Global mode:</b> {esc(labels.get(mode, mode))}\n\n"
        f"Commands: /on /off /auto /projects /status /quo /help /stop\n"
        f"Per-project: @ProjectName on/off/auto/clear"
    )

    last_update_id = 0

    # Drain pending Telegram updates from before startup
    drain = telegram_request("getUpdates", {"offset": 0, "limit": 100, "timeout": 0})
    if drain and drain.get("ok"):
        for update in drain.get("result", []):
            last_update_id = update["update_id"] + 1
            msg = update.get("message")
            if msg and msg.get("text", ""):
                if str(msg.get("chat", {}).get("id", "")) == str(CHAT_ID) and is_allowed_sender(msg.get("from")):
                    handle_command(msg["text"].strip())

    while True:
        # 1. Pick up new requests from hook scripts
        scan_pending_requests()

        # 2. Poll Telegram (short timeout so we check requests frequently)
        result = telegram_request("getUpdates", {
            "offset": last_update_id,
            "limit": 10,
            "timeout": 2,
            "allowed_updates": ["message", "callback_query"],
        })

        if result and result.get("ok"):
            for update in result.get("result", []):
                last_update_id = update["update_id"] + 1

                cb = update.get("callback_query")
                if cb:
                    handle_callback(cb)
                    continue

                msg = update.get("message")
                if not msg:
                    continue
                if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                    continue
                if not is_allowed_sender(msg.get("from")):
                    continue

                text = msg.get("text", "").strip()
                if not text:
                    continue

                # Commands first
                if text.startswith("/") or text.startswith("@"):
                    handle_command(text)
                    continue

                # Check if any active request is waiting for free-text input
                for rid, req in list(active_requests.items()):
                    if req["waiting_for_text"]:
                        edit_message(req["msg_id"],
                                     f"💬 <b>Answered:</b> {esc(text)}")
                        write_response(req["uuid"], {
                            "answer": text,
                            "responded_at": int(time.time()),
                        })
                        del active_requests[rid]
                        break

        # 3. Expire timed-out requests
        expire_stale_requests()


if __name__ == "__main__":
    main()
