#!/usr/bin/env python3
"""
run_bot.py — launcher + local deployment entry point for Jiro.

Responsible for:
1. Loading .env into the environment BEFORE any module import (all the bot
   modules read env vars at import time, so this ordering is load-bearing).
2. Optional Python version guard.
3. Spawning the bot with flag parsing passthrough.

Usage:
    python3 run_bot.py --loop                     # scan loop, alerts only
    python3 run_bot.py --loop --with-monitor      # scan + position monitor
    python3 run_bot.py --stop                     # send a one-shot "/stop"-style
                                                  # stop-request to any running loop
                                                  # by touching a pidfile/drop signal

Deployment (local, persistent):
    ./venv/bin/python run_bot.py --loop --with-monitor > ~/jiro.log 2>&1 &
    echo $! > ~/jiro.pid
    # check:  tail -f ~/jiro.log   |   kill $(cat ~/jiro.pid) to stop

The bot also listens for Telegram commands (/stop, /status, /config set ...)
once it's running, so you usually don't need the pidfile.
"""

import os
import sys
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(_HERE, ".env")


def _base64_to_base58_key(b64: str) -> str:
    """A Solana secret key is 64 raw bytes. Some setups store that key as
    base64 (88 chars, often with '==' padding) instead of the base58 string
    that solders.Keypair.from_base58_string() expects. Detect that and convert
    so the wallet loads as a normal base58 secret."""
    import base64
    b64 = b64.strip()
    # base64 alphabet only (may include +, /, =); a raw base58 key never does.
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", b64):
        return b64
    # sanity: decoded length should be 64 bytes (a Solana keypair) — that's
    # exactly what base64(64 bytes) yields. Re-encode to base58.
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return b64
    if len(raw) != 64:
        return b64  # not a solana key; leave as-is
    import base58 as _b58
    return _b58.b58encode(raw).decode("ascii")


def load_dotenv(path: str = ENV_FILE) -> None:
    """Tiny .env loader (no third-party dep). Loads KEY=VALUE lines, skips
    blank lines and comments, strips surrounding quotes. Does NOT override
    already-set environment variables (so shell exports win).

    Special case: SOLANA_PRIVATE_KEY may be stored as base64 — we normalize it
    to base58 so solders can parse it."""
    if not os.path.exists(path):
        print(f"[run_bot] no {path} — continuing with current env.")
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if not key:
                continue
            if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
                val = val[1:-1]
            if key not in os.environ:  # don't clobber an already-set var
                os.environ[key] = val

    # Normalize the wallet key from base64 to base58 if needed, in place.
    existing = os.environ.get("SOLANA_PRIVATE_KEY", "")
    if existing:
        os.environ["SOLANA_PRIVATE_KEY"] = _base64_to_base58_key(existing)


def _ensure_python_version():
    if sys.version_info < (3, 9):
        print(f"[run_bot] requires Python 3.9+, got {sys.version_info.major}.{sys.version_info.minor}")
        sys.exit(1)


def main():
    # Must happen before importing any bot module.
    load_dotenv()

    # Verify the critical secrets are present so startup failure is loud, not
    # the bot quietly running in alert-only mode when the operator expected more.
    missing = []
    if not os.environ.get("XAI_API_KEY"):
        missing.append("XAI_API_KEY")
    if not os.environ.get("RPC_URL"):
        missing.append("RPC_URL")
    if missing:
        print(f"[run_bot] ⚠️  MISSING env: {', '.join(missing)}. Bot will degrade; "
              "narrative/RPC features disabled.")
        # not fatal — bot can still run in degraded alert-only mode.

    args = sys.argv[1:]

    import gap_finder_bot

    # Rebuild args for the bot's own argparse by stripping our launcher flags.
    bot_args = [a for a in args if a not in ("--stop", "--status")]

    if "--stop" in args:
        # One-shot remote stop: set the shared stop flag used by gap_finder_bot.
        import bot_controller
        bot_controller.request_stop()
        print("[run_bot] stop-request set. NOTE: only effective if the running loop "
              "checks should_stop(); for a long-running process use the pidfile/kill "
              "or the Telegram /stop command instead.")
        sys.exit(0)

    # Insert the launcher path so `import config` etc. resolve to the repo.
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    sys.argv = ["gap_finder_bot.py"] + bot_args
    gap_finder_bot.main()


if __name__ == "__main__":
    main()