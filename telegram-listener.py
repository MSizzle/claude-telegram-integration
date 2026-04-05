#!/usr/bin/env python3
"""
Background Telegram listener for Claude Code approval commands.

Commands:
  /on          — global interactive mode
  /off         — global disabled
  /auto        — global auto-approve
  @Project on  — per-project override (on/off/auto/clear)
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

STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
CONFIG_FILE = os.path.expanduser("~/.claude/telegram-config.json")
PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
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
    # Prune stale active sessions on every write.
    now = int(time.time())
    state["active"] = {
        k: v for k, v in state.get("active", {}).items()
        if now - v.get("last_seen", 0) < 1800
    }
    with open(STATE_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)


# ── Telegram ──

def telegram_request(method, data):
    url = f"{API}/{method}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def send_message(text):
    telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    })


def send_message_with_markup(text, reply_markup):
    result = telegram_request("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    })
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def is_allowed_sender(from_obj):
    if not from_obj:
        return False
    return str(from_obj.get("id", "")) == str(CHAT_ID)


# ── Project picker keyboard ──

MODE_ICONS = {"on": "✅", "off": "🔇", "auto": "🚀"}
MODE_LABELS = {"on": "Interactive ✅", "off": "OFF 🔇", "auto": "Auto-approve 🚀"}


def collect_projects(state):
    """Union of recently-active and overridden projects, keyed by lowercase key."""
    now = int(time.time())
    seen = {}
    for k, info in state.get("active", {}).items():
        if now - info.get("last_seen", 0) < 1800:
            seen[k] = info.get("name", k)
    for k in state.get("projects", {}):
        seen.setdefault(k, k)
    return seen  # {key: display_name}


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
        # Truncate for button label (Telegram shows limited width anyway)
        short = name if len(name) <= 22 else name[:20] + "…"
        # Header row: project name as a no-op info button
        rows.append([{"text": f"— {short} —", "callback_data": f"pn:{key}"}])
        # Action row
        rows.append([
            {"text": "✅ on", "callback_data": f"pm:{key}:on"},
            {"text": "🔇 off", "callback_data": f"pm:{key}:off"},
            {"text": "🚀 auto", "callback_data": f"pm:{key}:auto"},
            {"text": "🧹 clear", "callback_data": f"pm:{key}:clear"},
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


def handle_callback(cb):
    """Handle inline keyboard button presses from the /projects picker."""
    cb_id = cb.get("id")

    if not is_allowed_sender(cb.get("from")):
        telegram_request("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": "Unauthorized",
        })
        return

    data = cb.get("data", "")
    msg = cb.get("message", {}) or {}
    msg_id = msg.get("message_id")

    # Set mode: pm:{key}:{mode}
    if data.startswith("pm:"):
        try:
            _, key, mode = data.split(":", 2)
        except ValueError:
            telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})
            return

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
            return

        telegram_request("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": toast,
        })
        if msg_id:
            refresh_projects_message(msg_id)
        return

    # Name label tapped: pn:{key} — just report current mode
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
        return

    # Refresh
    if data == "pr:refresh":
        if msg_id:
            refresh_projects_message(msg_id)
        telegram_request("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": "Refreshed",
        })
        return

    telegram_request("answerCallbackQuery", {"callback_query_id": cb_id})


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
        send_message_with_markup(
            format_projects_text(state),
            build_projects_keyboard(state),
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


def main():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

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
    flush = telegram_request("getUpdates", {"offset": -1, "limit": 1, "timeout": 0})
    if flush and flush.get("ok") and flush["result"]:
        last_update_id = flush["result"][-1]["update_id"] + 1

    while True:
        result = telegram_request("getUpdates", {
            "offset": last_update_id,
            "limit": 10,
            "timeout": 30,
            "allowed_updates": ["message", "callback_query"],
        })

        if not result or not result.get("ok"):
            time.sleep(5)
            continue

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
            if text:
                handle_command(text)


if __name__ == "__main__":
    main()
