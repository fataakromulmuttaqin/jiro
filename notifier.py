#!/usr/bin/env python3
"""
telegram.py — thin Telegram notifier for Jiro.

Two responsibilities:
1. Send message (text only, markdown) — used by gap_finder_bot for gap
   alerts, position opens/closes.
2. Fetch recent updates on startup, so we can confirm a chat_id is valid
   without spamming it.

Design:
- Lazy token validation. If TELEGRAM_BOT_TOKEN is unset OR chat_id is
  unset, every send() call is a no-op. The bot still runs fine without
  Telegram — just logs to stdout instead.
- All requests have a short timeout. Telegram API hiccups must NOT stall
  the trading loop.
- Errors are logged but never raised — a Telegram failure is a
  notification failure, not a trade failure.
"""

import os
import time
from typing import Optional, List, Dict, Any

import requests


_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_TIMEOUT_S = 8.0


def is_configured() -> bool:
    """True iff both bot token and chat id are present and non-empty."""
    return bool(_BOT_TOKEN) and bool(_CHAT_ID)


def send(text: str, parse_mode: str = "Markdown", disable_preview: bool = True) -> bool:
    """Send a message to the configured chat. Returns True on success,
    False on any failure (network, auth, rate limit). Never raises."""
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": _CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }, timeout=_TIMEOUT_S)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}", flush=True)
        return False


def get_recent_updates(limit: int = 5, timeout_s: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Fetch the most recent N updates. Useful for discovering chat_id
    when you just set up the bot (send /start to your bot first, then
    call this). Returns None on failure, [] if no updates.

    `timeout_s` is the Telegram long-poll timeout — set >0 for true
    long-polling, 0 for immediate response (default).
    """
    if not _BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"limit": limit, "timeout": timeout_s},
                         timeout=max(_TIMEOUT_S, timeout_s + 2))
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[telegram] getUpdates failed: {e}", flush=True)
        return None


def discover_chat_id() -> Optional[str]:
    """Convenience: return the chat_id of the most recent message addressed
    to this bot. Useful one-shot for setup. Returns None if no messages
    found yet (user needs to send /start first)."""
    updates = get_recent_updates(limit=5)
    if not updates:
        return None
    # most recent first
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None:
            return str(cid)
    return None


if __name__ == "__main__":
    # quick CLI: python3 telegram.py "hello"  → send a test message
    # python3 telegram.py --discover       → print discovered chat_id
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--discover":
        cid = discover_chat_id()
        if cid:
            print(f"discovered chat_id: {cid}")
        else:
            print("no chat_id found — send /start to your bot first, then re-run")
    elif len(sys.argv) >= 2:
        ok = send(sys.argv[1])
        print(f"send ok={ok}")
    else:
        print("usage: telegram.py 'text' | --discover")