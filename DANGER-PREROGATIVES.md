# Danger Mode Prerogatives

When operating in full-auto "danger" mode, use these prerogatives to make decisions autonomously without prompting the user.

## Project Context

This is a Telegram-based remote control for Claude Code. It handles approval hooks, question answering, per-project permission modes, dangerous-command detection, and project switching via inline keyboards.

**Core files:** `telegram-approve.py`, `telegram-question.py`, `telegram-listener.py`, `notify-telegram.sh`

## Decision Prerogatives

1. **Bias toward the working solution.** Pick the option that gets the feature working with minimal disruption to existing code. Don't over-engineer.
2. **Preserve existing behavior.** When a choice could break current functionality vs. extend it safely, always extend.
3. **Middle path on scope.** Never pick the option that does the least (lazy) or the most (gold-plating). Pick the reasonable middle ground.
4. **Favor simplicity over abstraction.** If one option adds a helper/utility/abstraction and another does it inline, prefer inline unless there's clear reuse.
5. **Security defaults matter.** For anything touching tokens, credentials, or shell commands, pick the more secure option.
6. **Respect the architecture.** Choices should align with the existing file-based IPC pattern and the hook-based integration with Claude Code.
7. **Ship it.** When options are roughly equivalent, pick whichever ships faster.

## How to Apply

When faced with a multiple-choice question:
- Read this file first.
- Evaluate each option against these prerogatives.
- Pick the one that best satisfies the most prerogatives.
- Respond with just the choice — no lengthy justification needed.
