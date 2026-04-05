# Claude Telegram Integration

Telegram-based remote control for [Claude Code](https://claude.com/claude-code): approve tool calls, answer questions, and manage per-project permission modes from your phone.

## Features

- **Permission approval** — Claude's `PermissionRequest` hook pops up an Approve/Deny button in Telegram instead of blocking in the terminal.
- **Question answering** — `AskUserQuestion` tool calls become inline-keyboard polls, including multi-select and free-text "Other" responses.
- **Per-project modes** — set any project to `on` (interactive), `off` (terminal only), or `auto` (auto-approve with notifications) independently.
- **Tap-to-configure `/projects`** — lists every active/overridden project with one-tap `on/off/auto/clear` buttons. No typing project names.
- **Red-flag detection** — even in auto mode, dangerous commands (`rm -rf`, `git push --force`, `sudo`, piped installs, etc.) escalate to manual approval.
- **Session queue (`/quo`)** — see which projects have active Claude sessions.
- **Background notifier (`notify-telegram.sh`)** — ping on `Stop` hook so you know when a long-running session finishes.

## Setup

### 1. Create a bot
- Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token.
- Message [@userinfobot](https://t.me/userinfobot) to get your numeric chat ID.

### 2. Store credentials
Either set environment variables:
```bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

Or create `~/.claude/telegram-config.json` (chmod 600):
```json
{
  "token": "your-token",
  "chat_id": "your-chat-id"
}
```

### 3. Wire up Claude Code hooks
Add to `~/.claude/settings.json`:
```json
{
  "hooks": {
    "PermissionRequest": [{
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/telegram-approve.py",
        "timeout": 130
      }]
    }],
    "PreToolUse": [{
      "matcher": "AskUserQuestion",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/telegram-question.py"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "/path/to/notify-telegram.sh 'Session finished'"
      }]
    }]
  }
}
```

### 4. Start the background listener
```bash
python3 /path/to/telegram-listener.py &
```
The listener handles chat commands (`/on`, `/auto`, `/projects`, etc.) and pauses itself while `telegram-approve.py` or `telegram-question.py` are actively polling to avoid Telegram API conflicts.

## Commands

| Command | Effect |
|---|---|
| `/on` | Global: interactive mode (Approve/Deny buttons) |
| `/off` | Global: disabled — fall back to terminal prompts |
| `/auto` | Global: auto-approve with summaries (dangerous commands still escalate) |
| `/projects` | Inline keyboard with one-tap `on/off/auto/clear` per project |
| `/status` | Show global mode + all per-project overrides |
| `/quo` | Show active Claude sessions (last 30 min) |
| `/help` | Command list |
| `/stop` | Stop the background listener |
| `@ProjectName on` | Per-project override: `on`, `off`, `auto`, or `clear` |

## Files

- `telegram-approve.py` — `PermissionRequest` hook. Sends approval prompts, detects risky commands, respects per-project modes.
- `telegram-question.py` — `AskUserQuestion` hook. Renders options as inline buttons; supports single-select, multi-select, and free-text fallback.
- `telegram-listener.py` — Long-running background process that handles chat commands and the `/projects` picker.
- `notify-telegram.sh` — Fire-and-forget notification helper for other hook events.
- `telegram-approve.example.json` — Example state-file schema. The real file is created automatically at `~/.claude/telegram-approve.json` on first run and is gitignored.

## Security notes

- **Never commit `telegram-config.json`, `.env`, or `telegram-approve.json`** — they're gitignored.
- The scripts verify every callback and text command against your configured `CHAT_ID` — taps from unknown senders are rejected.
- Dangerous-command detection in `telegram-approve.py` escalates to manual approval even under auto mode.
- If you rotate your bot token, update `~/.claude/telegram-config.json` only — the source files contain no credentials.

## License

MIT
