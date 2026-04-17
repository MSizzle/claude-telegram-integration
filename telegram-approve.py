#!/usr/bin/env python3
"""
Telegram-based permission approval for Claude Code.

Communicates with telegram-listener.py via file-based IPC:
  ~/.claude/telegram-pending/   — write request here
  ~/.claude/telegram-responses/ — poll for answer here

The listener is the sole Telegram API consumer. This script never
calls getUpdates or sendMessage directly.

Wired as a PermissionRequest hook.
"""

import sys
import json
import time
import os
import uuid
import fcntl
import re
import html as _html

TIMEOUT = 600  # 10 minutes — tap ⏳ +5m in Telegram to extend
STATE_FILE = os.path.expanduser("~/.claude/telegram-approve.json")
CONFIG_FILE = os.path.expanduser("~/.claude/telegram-config.json")
PID_FILE = os.path.expanduser("~/.claude/telegram-listener.pid")
PENDING_DIR = os.path.expanduser("~/.claude/telegram-pending")
RESPONSE_DIR = os.path.expanduser("~/.claude/telegram-responses")
HEARTBEAT_DIR = os.path.expanduser("~/.claude/telegram-heartbeats")
VALID_MODES = ("on", "off", "auto", "ffw")


# ── HTML formatting helpers ──

def esc(s):
    return _html.escape(str(s if s is not None else ""))


def code(s):
    return f"<code>{esc(s)}</code>"


def pre(s):
    return f"<pre>{esc(s)}</pre>"


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


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp, STATE_FILE)


def get_mode_for_project(project):
    state = load_state()
    # Global "off" is a master kill switch — overrides all per-project settings
    if state["default"] == "off":
        return "off"
    key = project.lower()
    if key in state["projects"]:
        return state["projects"][key]
    return state["default"]


def register_session(project, session_id):
    state = load_state()
    now = int(time.time())
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

# Two-tier heuristic (replaces the old blacklist).
#
# Tier 1 — Always Safe: Bash commands starting with any of these are
# bypassed (auto-approved) in auto and ffw modes. Must NOT match a
# Tier 2 pattern (Tier 2 always wins).
SAFE_PREFIXES = (
    "ls", "cat", "grep", "pwd",
    "git status", "git diff", "git log",
    "npm test", "pytest", "cargo test",
    "which",
)

# Tier 2 — High Danger: escalate to manual approval regardless of mode.
HIGH_DANGER_PATTERNS = [
    (r"\bsudo\b", "sudo — superuser execution"),
    (r"\brm\s+-[a-zA-Z]*[rRfF]", "rm -rf — recursive/forced delete"),
    (r"git\s+push\b[^|;&]*--force", "git push --force — can overwrite remote history"),
    (r"git\s+push\s+[^|;&]*\s-f\b", "git push -f — can overwrite remote history"),
    (r">\s*/dev/", "Redirect to /dev/ device"),
    (r"\bchmod\b", "chmod — changing file permissions"),
    (r"\bchown\b", "chown — changing file ownership"),
    (r"\|\s*(bash|sh|zsh|ksh|dash)\b", "Pipe into shell interpreter"),
]


def detect_risks(hook_input):
    """High-danger reasons only — drives the 🚩 danger view and manual-approval gating."""
    risks = []
    tool = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool == "Bash":
        cmd = tool_input.get("command", "")
        for pattern, reason in HIGH_DANGER_PATTERNS:
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

    return list(dict.fromkeys(risks))


def classify(hook_input):
    """Return (tier, risks). tier ∈ {'safe', 'normal', 'high_danger'}."""
    risks = detect_risks(hook_input)
    if risks:
        return "high_danger", risks

    tool = hook_input.get("tool_name", "")
    if tool == "Bash":
        cmd = hook_input.get("tool_input", {}).get("command", "").lstrip()
        for prefix in SAFE_PREFIXES:
            if cmd == prefix or cmd.startswith(prefix + " "):
                return "safe", []
    return "normal", []


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


# ── IPC helpers ──

def listener_alive():
    """Check if the listener process is running."""
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


def read_heartbeat(req_uuid):
    """Read the heartbeat file written by the listener when user taps ⏳ +5m."""
    path = os.path.join(HEARTBEAT_DIR, f"{req_uuid}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return None


def poll_response(req_uuid, timeout):
    """Poll for a response file. Returns parsed JSON or None on timeout.

    Honors heartbeat extensions: if the listener writes a heartbeat file
    with a later deadline (user tapped ⏳ +5m), we extend the wait.
    """
    path = os.path.join(RESPONSE_DIR, f"{req_uuid}.json")
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    response = json.load(f)
                os.remove(path)
                return response
            except (json.JSONDecodeError, IOError):
                time.sleep(0.2)
                continue
        hb = read_heartbeat(req_uuid)
        if hb:
            new_deadline = hb.get("deadline")
            if isinstance(new_deadline, (int, float)) and new_deadline > deadline:
                deadline = new_deadline
        time.sleep(0.5)
    return None


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

    # Listener must be running for IPC
    if not listener_alive():
        sys.exit(0)

    tier, risks = classify(hook_input)

    # Tier 2 (high danger) ALWAYS escalates to manual approval, even in auto/ffw.
    # Tier 1 (safe) and normal fall through to the mode's auto-approve path.
    if tier != "high_danger":
        if mode == "ffw":
            print(json.dumps(approve_output()))
            sys.exit(0)
        if mode == "auto":
            _, description = describe_action(hook_input)
            write_request({
                "type": "notify",
                "id": uuid.uuid4().hex,
                "message_html": f"⚡ <b>Auto-approved</b> — {code(project)}\n\n{description}",
                "created_at": int(time.time()),
            })
            print(json.dumps(approve_output()))
            sys.exit(0)

    # Interactive approval — send request, wait for response
    request_id = uuid.uuid4().hex[:8]
    req_uuid = uuid.uuid4().hex
    message = format_permission_message(hook_input, risks=risks)

    approve_label = "✅ Approve Anyway" if risks else "✅ Approve"
    keyboard = {
        "inline_keyboard": [[
            {"text": approve_label, "callback_data": f"approve:{request_id}"},
            {"text": "❌ Deny", "callback_data": f"deny:{request_id}"},
        ]]
    }

    write_request({
        "type": "approve",
        "id": req_uuid,
        "project": project,
        "message_html": message,
        "keyboard": keyboard,
        "request_id": request_id,
        "timeout": TIMEOUT,
        "created_at": int(time.time()),
    })

    response = poll_response(req_uuid, TIMEOUT)

    if response and response.get("answer") == "approve":
        print(json.dumps(approve_output()))
        sys.exit(0)
    else:
        reason = "dangerous command" if tier == "high_danger" else "Telegram"
        print(f"Denied via {reason}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
