#!/bin/bash
# Simple Telegram notifier. Credentials loaded from env or ~/.claude/telegram-config.json.

CONFIG="$HOME/.claude/telegram-config.json"

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
  if [ -f "$CONFIG" ]; then
    TOKEN="${TOKEN:-$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("token",""))' "$CONFIG")}"
    CHAT_ID="${CHAT_ID:-$(/usr/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("chat_id",""))' "$CONFIG")}"
  fi
fi

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
  echo "notify-telegram: credentials missing (set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or create $CONFIG)" >&2
  exit 0
fi

PROJECT=$(basename "$PWD")
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d chat_id="${CHAT_ID}" \
  --data-urlencode "text=🔔 [$PROJECT] $1" > /dev/null
