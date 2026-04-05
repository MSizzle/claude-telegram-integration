#!/usr/bin/env python3
"""
Telegram-based permission approval for Claude Code.

Modes (controlled via Telegram commands):
  /on         — global interactive (Approve/Deny buttons)
  /off        — global disabled (terminal only)
  /auto       — global auto-approve with summaries
  @Project on — per-project override (on/off/auto/clear)
  /quo        — show all active sessions and their modes
  /status     — show global + per-project modes
  /help       — show commands

Red flag detection: dangerous commands escalate to interactive
even in auto mode, with a prominent warning.

Wired as a PermissionRequest hook.
"""

import sys
import json
import time
import urllib.request
import os
import uuid
import fcntl
import re
import signal
import html as _html

POLL_INTERVAL = 1.5
TIMEOUT = 120
STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
CONFIG_FILE = os.path.expanduser("~/.claude/telegram-config.json")
LISTENER_PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
POLL_LOCK_FILE = os.path.expanduser("~/.claude/telegram-poll.lock")
VALID_MODES = ("on", "off", "auto")


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
            f"or create {CONFIG_FILE} with {{\"token\":..., \"chat_id\":...}}.",
            file=sys.stderr,
        )
        sys.exit(0)
    return token, chat_id


TOKEN, CHAT_ID = load_credentials()
API = f"https://api.telegram.org/bot{TOKEN}"


# ── HTML formatting helpers ──

def esc(s):
    """Escape user-supplied string for HTML parse_mode."""
    return _html.escape(str(s if s is not None else ""))


def code(s):
    return f"<code>{esc(s)}</code>"


def pre(s):
    return f"<pre>{esc(s)}</pre>"


def b(s):
    return f"<b>{esc(s)}</b>"


def i(s):
    return f"<i>{esc(s)}</i>"


# ── Listener coordination ──

def pause_listener():
    if os.path.exists(LISTENER_PID_FILE):
        try:
            with open(LISTENER_PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGSTOP)
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


class PollSession:
    """Serialize getUpdates across approve/question and pause listener.

    Guarantees resume_listener + lock release on every exit path.
    """

    def __init__(self):
        self.lock_fh = None
        self.listener_pid = None

    def __enter__(self):
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


# ── Risk Detection ──

SYSTEM_PATHS = (
    "/etc/", "/usr/", "/System/", "/Library/", "/var/",
    "/bin/", "/sbin/", "/opt/", "/private/",
)

SENSITIVE_FILES = (
    ".env", ".ssh/", ".aws/", ".gnupg/", ".npmrc",
    "credentials", "keychain", "id_rsa", "id_ed25519",
    ".git/config", ".netrc", "secrets",
)

DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[a-zA-Z]*[rf])", "Recursive/forced file deletion"),
    (r"\brm\s+.*(/|~|\$HOME)", "Deleting files from a broad path"),
    (r"git\s+push\s+.*--force", "Force pushing (can overwrite remote history)"),
    (r"git\s+push\s+-f\b", "Force pushing (can overwrite remote history)"),
    (r"git\s+reset\s+--hard", "Hard reset (discards all uncommitted changes)"),
    (r"git\s+clean\s+.*-[a-zA-Z]*f", "Force cleaning untracked files"),
    (r"git\s+checkout\s+\.\s*$", "Discarding all local changes"),
    (r"git\s+branch\s+.*-D\b", "Force deleting a branch"),
    (r"\bsudo\b", "Running with superuser privileges"),
    (r"\bchmod\s+777\b", "Setting world-writable permissions"),
    (r"\bchown\b.*(/etc|/usr|/System)", "Changing ownership of system files"),
    (r"\bdd\s+", "Low-level disk write (dd)"),
    (r"\bmkfs\b", "Formatting a filesystem"),
    (r"\bfdisk\b", "Modifying disk partitions"),
    (r"\bdiskutil\s+(erase|partition)", "Erasing or partitioning a disk"),
    (r"\bkill\s+-9\b", "Force killing a process"),
    (r"\bkillall\b", "Killing processes by name"),
    (r"\bpkill\b", "Killing processes by pattern"),
    (r"curl\s.*\|\s*(sh|bash|zsh)", "Piping downloaded script to shell"),
    (r"wget\s.*\|\s*(sh|bash|zsh)", "Piping downloaded script to shell"),
    (r"curl\s.*\|\s*python", "Piping downloaded script to Python"),
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "Dropping a database object"),
    (r"\bTRUNCATE\b", "Truncating a table"),
    (r"\bDELETE\s+FROM\b.*WHERE\s+1\s*=\s*1", "Deleting all rows"),
    (r"\bDELETE\s+FROM\b(?!.*WHERE)", "DELETE without WHERE clause"),
    (r"\bunset\b.*(PATH|HOME|USER|SHELL)", "Unsetting critical env variable"),
    (r">\s*/dev/sd", "Writing directly to a disk device"),
    (r"/etc/hosts\b", "Modifying hosts file"),
    (r"\biptables\b", "Modifying firewall rules"),
    (r"npm\s+publish\b", "Publishing a package to npm"),
    (r"npm\s+unpublish\b", "Unpublishing a package from npm"),
]


def detect_risks(hook_input):
    risks = []
    tool = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    cwd = hook_input.get("cwd", "")

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        for pattern, reason in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                risks.append(reason)
        for sp in SYSTEM_PATHS:
            if sp in cmd and any(w in cmd for w in ("rm", "mv", "chmod", "chown", "write", ">")):
                risks.append(f"Modifying system path {sp}")
                break
        for sf in SENSITIVE_FILES:
            if sf in cmd and any(w in cmd for w in ("rm", "mv", "cat", "cp", ">")):
                risks.append(f"Accessing sensitive file {sf}")
                break

    elif tool in ("Write", "Edit", "MultiEdit"):
        fp = tool_input.get("file_path", "")
        for sp in SYSTEM_PATHS:
            if fp.startswith(sp):
                risks.append(f"Modifying file in system path {sp}")
                break
        for sf in SENSITIVE_FILES:
            if sf in fp:
                risks.append(f"Modifying sensitive file containing {sf}")
                break
        if cwd and fp and not fp.startswith(cwd):
            home = os.path.expanduser("~")
            safe_outside = (
                os.path.join(home, ".claude/"),
                os.path.join(home, ".config/"),
            )
            if not any(fp.startswith(s) for s in safe_outside):
                risks.append("Writing outside project directory")

    return list(dict.fromkeys(risks))


# ── State Management ──

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
    with open(STATE_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)


def get_mode_for_project(project):
    state = load_state()
    key = project.lower()
    if key in state["projects"]:
        return state["projects"][key]
    return state["default"]


def register_session(project, session_id):
    state = load_state()
    now = int(time.time())
    # Prune stale entries (older than 30 minutes)
    state["active"] = {
        k: v for k, v in state.get("active", {}).items()
        if now - v.get("last_seen", 0) < 1800
    }
    state["active"][project.lower()] = {
        "name": project,
        "session_id": session_id,
        "last_seen": now,
    }
    save_state(state)


# ── Telegram API ──

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


# ── Describe Actions ──

def describe_action(hook_input):
    tool = hook_input.get("tool_name", "Unknown")
    project = os.path.basename(hook_input.get("cwd", ""))
    tool_input = hook_input.get("tool_input", {})

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            return project, f"Ran a command: {esc(desc)}"
        if cmd.startswith("cd "):
            return project, f"Changed directory to {code(cmd[3:])}"
        if cmd.startswith(("npm ", "git ", "pip ", "python")):
            return project, f"Ran {code(cmd[:80])}"
        if "&&" in cmd:
            parts = [p.strip().split()[0] for p in cmd.split("&&") if p.strip()]
            return project, f"Ran a chain of commands: {esc(', '.join(parts))}"
        if len(cmd) > 80:
            return project, f"Ran command: {code(cmd[:80] + '...')}"
        return project, f"Ran command: {code(cmd)}"
    elif tool == "Edit":
        fp = os.path.basename(tool_input.get("file_path", ""))
        return project, f"Edited {code(fp)} — replaced some code"
    elif tool == "MultiEdit":
        fp = os.path.basename(tool_input.get("file_path", ""))
        edits = tool_input.get("edits", [])
        return project, f"Made {len(edits)} edit(s) to {code(fp)}"
    elif tool == "Write":
        fp = os.path.basename(tool_input.get("file_path", ""))
        return project, f"Created/overwrote file {code(fp)}"
    elif tool == "Read":
        fp = os.path.basename(tool_input.get("file_path", ""))
        return project, f"Read file {code(fp)}"
    elif tool == "Agent":
        desc = tool_input.get("description", "launched a sub-agent")
        return project, f"Launched agent: {esc(desc)}"
    elif tool == "WebFetch":
        url = tool_input.get("url", "")
        return project, f"Fetched URL: {code(url[:60])}"
    elif tool == "WebSearch":
        query = tool_input.get("query", "")
        return project, f"Searched the web: {esc(query)}"
    elif tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else "unknown"
        method = parts[2] if len(parts) > 2 else tool
        return project, f"Called MCP tool {code(method)} on {code(server)}"
    else:
        raw = json.dumps(tool_input)
        if len(raw) > 100:
            raw = raw[:100] + "..."
        return project, f"Used {code(tool)}: {esc(raw)}"


def format_permission_message(hook_input, risks=None):
    tool = hook_input.get("tool_name", "Unknown")
    project = os.path.basename(hook_input.get("cwd", ""))
    tool_input = hook_input.get("tool_input", {})

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        detail = pre(cmd)
    elif tool in ("Edit", "MultiEdit"):
        fp = tool_input.get("file_path", "")
        detail = f"File: {code(fp)}"
    elif tool == "Write":
        fp = tool_input.get("file_path", "")
        detail = f"New file: {code(fp)}"
    elif tool == "Agent":
        desc = tool_input.get("description", "")
        detail = f"Agent: {esc(desc)}"
    else:
        raw = json.dumps(tool_input, indent=2)
        if len(raw) > 200:
            raw = raw[:200] + "..."
        detail = pre(raw)

    if risks:
        msg = "🚩🚩🚩 <b>DANGEROUS — REVIEW CAREFULLY</b> 🚩🚩🚩\n\n"
        for r in risks:
            msg += f"⚠️ {esc(r)}\n"
        msg += f"\n<b>Project:</b> {code(project)}\n"
        msg += f"<b>Tool:</b> {code(tool)}\n\n"
        msg += detail
    else:
        msg = "🔐 <b>Permission Required</b>\n\n"
        msg += f"<b>Project:</b> {code(project)}\n"
        msg += f"<b>Tool:</b> {code(tool)}\n\n"
        msg += detail

    return msg


# ── Project picker callback handling ──
#
# The /projects inline keyboard is rendered by telegram-listener.py, but
# callbacks can arrive while this script is polling (the listener is paused
# during poll). Handle them inline so taps work even mid-prompt.

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
        rows.append([{"text": f"— {short} —", "callback_data": f"pn:{key}"}])
        rows.append([
            {"text": "✅ on", "callback_data": f"pm:{key}:on"},
            {"text": "🔇 off", "callback_data": f"pm:{key}:off"},
            {"text": "🚀 auto", "callback_data": f"pm:{key}:auto"},
            {"text": "🧹 clear", "callback_data": f"pm:{key}:clear"},
        ])
    rows.append([{"text": "🔄 Refresh", "callback_data": "pr:refresh"}])
    return {"inline_keyboard": rows}


def handle_project_callback(cb):
    """Return True if this callback was a project-picker event and was handled."""
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


# ── Command Handling ──

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
        return True
    elif lower == "/auto":
        state = load_state()
        state["default"] = "auto"
        save_state(state)
        send_message("🚀 <b>Global: Auto-approve ON</b>\n\nAll projects auto-approved with summaries (unless overridden).\n\n🚩 Dangerous commands always require manual approval.")
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
        msg += "\n🚩 Dangerous commands always escalate to interactive."
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
    elif lower == "/help":
        send_message(
            "🤖 <b>Commands</b>\n\n"
            "/on — global interactive mode\n"
            "/off — global disabled\n"
            "/auto — global auto-approve\n"
            "@Project on — per-project (on/off/auto/clear)\n"
            "/status — show all modes\n"
            "/quo — active sessions queue\n"
            "/help — this message"
        )
        return True

    return False


# ── Polling ──

def poll_for_response(request_id, msg_id):
    start = time.time()
    last_update_id = 0

    flush = telegram_request("getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
    if flush and flush.get("ok") and flush["result"]:
        last_update_id = flush["result"][-1]["update_id"] + 1

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
                # Only trust messages from the configured chat
                if str(msg.get("chat", {}).get("id", "")) == str(CHAT_ID) and is_allowed_sender(msg.get("from")):
                    handle_command(msg["text"])
                continue

            cb = update.get("callback_query")
            if not cb:
                continue

            # Reject callbacks from unauthorized senders
            if not is_allowed_sender(cb.get("from")):
                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb.get("id"),
                    "text": "Unauthorized",
                })
                continue

            data = cb.get("data", "")
            cb_id = cb.get("id")

            # Project-picker buttons from /projects keyboard
            if handle_project_callback(cb):
                continue

            if data.endswith(f":{request_id}"):
                action = data.split(":")[0]

                telegram_request("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "Approved ✅" if action == "approve" else "Denied ❌",
                })

                status = "✅ <b>Approved</b>" if action == "approve" else "❌ <b>Denied</b>"
                telegram_request("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": f"{status}\n\n(responded in {int(time.time() - start)}s)",
                    "parse_mode": "HTML",
                })

                return action

            telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})

        time.sleep(POLL_INTERVAL)

    telegram_request("editMessageText", {
        "chat_id": CHAT_ID,
        "message_id": msg_id,
        "text": "⏰ <b>Timed out</b> — no response received",
        "parse_mode": "HTML",
    })
    return "deny"


def approve_output():
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


# ── Main ──

def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("Failed to read hook input", file=sys.stderr)
        sys.exit(2)

    project = os.path.basename(hook_input.get("cwd", ""))
    session_id = hook_input.get("session_id", "")

    register_session(project, session_id)

    mode = get_mode_for_project(project)

    # OFF — fall through to terminal
    if mode == "off":
        sys.exit(0)

    risks = detect_risks(hook_input)

    # AUTO mode, safe command: no polling needed, no lock/pause required.
    if mode == "auto" and not risks:
        _, description = describe_action(hook_input)
        send_message(f"⚡ <b>Auto-approved</b> — {code(project)}\n\n{description}")
        print(json.dumps(approve_output()))
        sys.exit(0)

    # All other paths need the poll lock and listener pause.
    with PollSession():
        request_id = uuid.uuid4().hex[:8]
        message = format_permission_message(hook_input, risks=risks)

        approve_label = "✅ Approve Anyway" if risks else "✅ Approve"
        keyboard = {
            "inline_keyboard": [[
                {"text": approve_label, "callback_data": f"approve:{request_id}"},
                {"text": "❌ Deny", "callback_data": f"deny:{request_id}"},
            ]]
        }

        msg_id = send_message(message, reply_markup=keyboard)
        if not msg_id:
            # Couldn't reach Telegram — fall through to terminal
            sys.exit(0)

        decision = poll_for_response(request_id, msg_id)

    if decision == "approve":
        print(json.dumps(approve_output()))
        sys.exit(0)
    else:
        reason = "dangerous command" if risks else "Telegram"
        print(f"Denied via {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
