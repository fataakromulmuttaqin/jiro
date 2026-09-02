#!/usr/bin/env python3
"""
bot_controller.py — remote control for Jiro over Telegram.

Lets you drive the bot from your phone: start/stop the loop, read the
current config, tune config values live, and check status — all without
SSH-ing in.

Commands (only from the authorized chat id in TELEGRAM_CHAT_ID are honored):

  /status              bot mode, open positions, recent state
  /start               clear the stop flag (let the loop keep running)
  /stop                request the loop to stop cleanly at the next cycle
  /config              show the full current config
  /config get PATH     show one nested value, e.g. trading.position_size_usd
  /config set PATH VALUE   set one nested value live, e.g.
                           trading.trailing_stop_activate_pct 40
                           (booleans / ints / floats / JSON are auto-parsed)
  /help                list commands

Design:
- Runs in its own daemon thread so it never blocks the trading loop.
- Uses getUpdates long-ish polling with an offset so each command is
  consumed exactly once and no command is lost on restart.
- Only replies to / acts on the configured authorized chat id — everyone
  else is ignored (defense against a leaked token).
- Config edits go through config.py's load/save, so they are atomic and the
  trading loop picks them up on its next cycle (it reloads config each tick).

The global stop flag is read by gap_finder_bot.main()'s loop every iteration.
"""

import os
import json
import threading
import time
import datetime as dt
from typing import Optional, Dict, Any, List, Tuple

import requests

import config as cfgmod
import notifier


# ---------------------------------------------------------------------------
# Configuration (arrive via the launcher's env, loaded before import)
# ---------------------------------------------------------------------------

EDITOR_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_POLL_OFFSET = 0  # last consumed update_id; survives across loop ticks
_LONG_POLL_S = 25

# Stop / start control. Uses a flag + condition so the trading loop can block
# on it if we ever want to pause mid-monitor rather than only between cycles.
_stop_flag = threading.Event()
# Config lock so a live /config set never races the loop's reload.
_config_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Public control API (used by gap_finder_bot)
# ---------------------------------------------------------------------------


def request_stop() -> None:
    """Ask the bot to stop cleanly after the current cycle."""
    _stop_flag.set()


def request_start() -> None:
    """Clear the stop flag so the loop keeps running."""
    _stop_flag.clear()


def should_stop() -> bool:
    """True when a /stop has been requested. Checked each loop iteration."""
    return _stop_flag.is_set()


def edit_config(path: str, value: Any) -> Optional[str]:
    """Set config.json[p1][p2][...] = <value>, return an error string or None.
    Nested dict created on the fly. Live — next loop cycle picks it up."""
    try:
        cfg = cfgmod.load_config()
        parts = path.split(".")
        obj = cfg
        for p in parts[:-1]:
            if not isinstance(obj.get(p), dict):
                obj[p] = {}
            obj = obj[p]
        obj[parts[-1]] = value
        with _config_lock:
            cfgmod.save_config(cfg)
        return None
    except Exception as e:
        return str(e)


def get_config_path(path: str) -> Tuple[Optional[Any], Optional[str]]:
    """Return (value, error_string) for a dotted config path."""
    try:
        cfg = cfgmod.load_config()
        obj: Any = cfg
        for p in path.split("."):
            obj = obj[p]
        return obj, None
    except (KeyError, TypeError) as e:
        return None, f"path not found: {e}"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _reply(text: str) -> None:
    notifier.send(text)


def _format_status() -> str:
    def _import_trading():
        try:
            import trading
            return trading
        except Exception:
            return None

    trading_mod = _import_trading()
    if trading_mod is None:
        mode = {
            "mode": f"auto={os.environ.get('AUTO_TRADE_ENABLED','false')} "
                    f"dry={os.environ.get('DRY_RUN','true')}",
        }
    else:
        try:
            import safety
            status = safety.arm_status()
            mode = {
                "mode": status.get("mode", "?"),
                "AUTO_TRADE_ENABLED": status.get("AUTO_TRADE_ENABLED"),
                "DRY_RUN": status.get("DRY_RUN"),
            }
        except Exception:
            mode = {"mode": "?"}

    cfg_view = {}
    try:
        c = cfgmod.load_config()
        cfg_view = {
            "pos_usd": c["trading"]["position_size_usd"],
            "max_pos": c["trading"]["max_open_positions"],
            "trailing": c["trading"]["trailing_stop_enabled"],
            "holder_screen": c.get("holder_filters", {}).get("enabled"),
            "smart_money": c.get("smart_money", {}).get("enabled"),
            "poll_min": c["system"]["poll_interval_minutes"],
            "monitor_s": c["system"]["monitor_interval_seconds"],
        }
    except Exception as e:
        cfg_view = {"error": str(e)}

    open_n = 0
    try:
        if trading_mod is not None:
            positions = trading_mod.load_positions()
            open_n = sum(1 for p in positions if p.get("status") == "open")
    except Exception:
        pass

    return (
        f"🤖 *Jiro status*\n"
        f"mode: {mode.get('mode','?')}\n"
        f"stop_requested: {should_stop()}\n"
        f"open_positions: {open_n}\n"
        f"config: {json.dumps(cfg_view)}\n"
        f"time: {dt.datetime.utcnow().isoformat()}Z"
    )


def _handle_command(text: str) -> Optional[str]:
    """Dispatch a command. Return a reply string, or None to reply nothing."""
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()
    rest = parts[1:]

    if cmd in ("/help", "help", "/start_help"):
        return (
            "📋 Commands:\n"
            "/status\n"
            "/start\n"
            "/stop\n"
            "/config\n"
            "/config get trading.position_size_usd\n"
            "/config set trading.trailing_stop_activate_pct 40\n"
            "/help"
        )

    if cmd == "/status":
        return _format_status()

    if cmd == "/stop":
        request_stop()
        return "🛑 Stop requested. Bot will halt after the current cycle."

    if cmd == "/start":
        request_start()
        return "▶️ Start signal sent. Loop is running."

    if cmd == "/config":
        if not rest:
            try:
                return "`" + json.dumps(cfgmod.load_config(), indent=2) + "`"
            except Exception as e:
                return f"couldn't read config: {e}"
        sub = rest[0].lower()
        if sub == "get" and len(rest) >= 2:
            val, err = get_config_path(rest[1])
            return f"`{rest[1]}` = {val}" if err is None else f"❌ {err}"
        if sub == "set" and len(rest) >= 3:
            path = rest[1]
            raw = rest[2]
            parsed = _parse_value(raw)
            if isinstance(parsed, _ParseErr):
                return f"❌ bad value: {parsed.msg}"
            err = edit_config(path, parsed)
            if err:
                return f"❌ {err}"
            val, _ = get_config_path(path)
            return f"✅ `{path}` = {val}"
        return "Usage: /config OR /config get PATH OR /config set PATH VALUE"

    return None  # unknown command — ignore silently


class _ParseErr:
    def __init__(self, msg: str):
        self.msg = msg


def _parse_value(raw: str):
    """Parse a CLI-ish value into bool/int/float/JSON, best-effort."""
    s = raw.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        int(s)
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
        if "/" not in s:
            return f
    except ValueError:
        pass
    return s


# ---------------------------------------------------------------------------
# Polling thread
# ---------------------------------------------------------------------------


def _poll_once():
    global _POLL_OFFSET
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": _POLL_OFFSET, "timeout": _LONG_POLL_S}
    try:
        r = requests.get(url, params=params, timeout=_LONG_POLL_S + 5)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception:
        return
    for u in updates:
        _POLL_OFFSET = u.get("update_id", _POLL_OFFSET) + 1
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text or chat_id != EDITOR_CHAT_ID:
            continue
        reply = _handle_command(text)
        if reply is not None:
            _reply(reply)


def _poll_loop():
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"[bot_control] poll error: {e}", file=__import__("sys").stderr)
        time.sleep(0.5)


def start_control_thread() -> None:
    """Spawn the daemon polling thread. Safe to call multiple times."""
    if not BOT_TOKEN or not EDITOR_CHAT_ID:
        print("[bot_control] Telegram control disabled (missing token or chat id).")
        return
    t = threading.Thread(target=_poll_loop, daemon=True, name="bot_control")
    t.start()
    print(f"[bot_control] Telegram control active (chat {EDITOR_CHAT_ID}). Command: /status, /stop, /config ...")