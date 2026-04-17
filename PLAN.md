# Refactor: File-based IPC Architecture

## Goal
Eliminate all `getUpdates` contention by making `telegram-listener.py` the sole Telegram poller, with `telegram-approve.py` and `telegram-question.py` communicating via request/response JSON files instead of calling the Telegram API directly.

## Steps

### 1. Define the IPC protocol and directories
- Create two directories: `~/.claude/telegram-pending/` (hook → listener) and `~/.claude/telegram-responses/` (listener → hook)
- Request file: `{uuid}.json` containing `{type: "approve"|"question", project, message_html, keyboard, request_id, created_at}`
- Response file: `{uuid}.json` containing `{answer: "approve"|"deny"|"Option label"|free text, responded_at}`

### 2. Rewrite `telegram-listener.py` main loop (~line 495)
- Reduce `getUpdates` timeout from 30s to 2-3s so the loop can also check for pending request files frequently
- Each iteration: (a) poll `getUpdates` with short timeout, (b) scan `~/.claude/telegram-pending/` for new request files
- When a request file is found: send the Telegram message with keyboard, store a mapping of `{request_id → uuid}` in memory, move the request file to a "sent" state (or delete it)
- When a callback arrives: check if it matches a pending request's `request_id` → write `~/.claude/telegram-responses/{uuid}.json` with the answer
- When a text message arrives during a pending question (waiting_for_text state): write it as the response
- Keep all existing command handling (`/on`, `/off`, `/auto`, `/projects`, etc.) unchanged
- Handle request timeouts: if a request has been pending >120s (approve) or >180s (question), write a timeout response and clean up

### 3. Rewrite `telegram-approve.py` — strip down to ~60 lines
- **Remove:** all `getUpdates` calls, `PollSession`, `pause_listener`/`resume_listener`, `rescue_stale_listener`, `telegram_request`, `send_message`, `poll_for_response`, lock file handling, 409 retry logic
- **Keep:** `load_state`, `get_mode_for_project`, `register_session`, `detect_risks`, `format_permission_message`, `describe_action`
- **New flow:** read hook stdin → check mode → if auto+safe, write a "notify" request (fire-and-forget) and approve → if interactive, write a request file to `~/.claude/telegram-pending/{uuid}.json` → poll `~/.claude/telegram-responses/{uuid}.json` every 0.5s until it appears or timeout → read decision → exit 0 or 2
- Auto-approve notification: write a request with `type: "notify"` (no response needed), the listener sends it as a one-way message

### 4. Rewrite `telegram-question.py` — strip down to ~60 lines
- **Remove:** same Telegram API / polling / lock / SIGSTOP machinery
- **Keep:** `load_state`, `get_mode_for_project`, `format_question`, `build_keyboard`, `build_multi_keyboard`
- **New flow:** read hook stdin → check mode → write request file with question data, keyboard, and multi-select flag → poll for response file → read answer → exit 2 with feedback
- For multi-select: the listener handles the toggle/done logic internally and only writes the final answer to the response file

### 5. Clean up dead code
- Remove `POLL_LOCK_FILE`, `PAUSE_TIMESTAMP_FILE`, `LISTENER_PID_FILE` references from approve and question scripts
- Remove `PID_FILE` write from listener (no longer needed for SIGSTOP coordination)
- Remove `signal.SIGSTOP`/`SIGCONT` usage everywhere
- Delete `~/.claude/telegram-poll.lock` and `~/.claude/telegram-listener-paused-at` if they exist

### 6. Update hooks config in `~/.claude/settings.json`
- No changes needed — the hook commands stay the same, just the scripts' internals change
- Optionally reduce timeouts since file polling is faster than network polling

## Risks & gotchas

- **Listener must be running** — today, if the listener is down, approve/question scripts fall back to talking to Telegram directly. With the new design, if the listener isn't running, requests sit in `~/.claude/telegram-pending/` forever and the hook script times out. Need to detect this (check PID file or listener heartbeat) and fall through to terminal gracefully.
- **Multi-select toggle state** — currently managed in `poll_for_answer` inside the question script. Must move this stateful logic (tracking which options are selected, updating the keyboard) into the listener. This is the most complex piece to port.
- **Race on response file** — the hook script could read a partially-written response file. Use atomic write (write to `.tmp`, then `os.replace`) same pattern already used for `telegram-approve.json`.
- **Stale request cleanup** — if a hook script crashes before reading its response, orphan files accumulate. The listener should clean up files older than ~5 minutes.
- **Auto-approve notifications** are fire-and-forget today (the approve script sends the message directly). With IPC, there's a slight delay while the listener picks up the request file. This is fine but noticeable if the listener's poll cycle is slow.

## Open questions

- Should the listener also handle **Telegram Forum Topics** (one thread per project) for visual separation, or save that for a follow-up?
- Should there be a fallback where approve/question scripts talk directly to Telegram if the listener is confirmed dead? Or just timeout and fall through to terminal?

## Proposed approach

The key decision is using the filesystem as the IPC mechanism rather than sockets or pipes. Files are simpler (no connection management), naturally persistent (survives script crashes), and debuggable (you can `cat` the request/response files). The listener's main loop becomes a dual-poll: short-timeout `getUpdates` (2-3s) interleaved with a directory scan for request files. This keeps Telegram responsiveness high while checking for new requests frequently. The hook scripts become thin clients that just write a file, wait for a response file, and exit — all the Telegram API complexity lives in one place.
