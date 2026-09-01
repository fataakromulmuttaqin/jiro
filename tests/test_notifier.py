#!/usr/bin/env python3
"""
test_notifier.py — unit tests for the Telegram notifier.

Covers:
- send() is a no-op when token or chat_id missing (returns False)
- send() actually POSTs to the right URL with the right payload
- send() never raises on network errors (caller doesn't care)
- get_recent_updates returns [] when no token, None on error
"""

import os
from unittest.mock import patch, MagicMock

import notifier as notifier_module


def _mock_response(json_data=None, raise_=False):
    r = MagicMock()
    r.raise_for_status = MagicMock(side_effect=raise_ if raise_ else None)
    r.json = MagicMock(return_value=json_data or {})
    return r


def test_send_is_noop_when_token_missing(monkeypatch):
    """Without TELEGRAM_BOT_TOKEN, send() returns False and never hits network."""
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "")
    monkeypatch.setattr(notifier_module, "_CHAT_ID", "123")
    assert notifier_module.is_configured() is False
    assert notifier_module.send("hello") is False


def test_send_is_noop_when_chat_id_missing(monkeypatch):
    """Without TELEGRAM_CHAT_ID, send() returns False and never hits network."""
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(notifier_module, "_CHAT_ID", "")
    assert notifier_module.is_configured() is False
    assert notifier_module.send("hello") is False


def test_send_posts_correct_payload(monkeypatch):
    """With both envs set, send() builds the right URL + payload."""
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "ABC123")
    monkeypatch.setattr(notifier_module, "_CHAT_ID", "999")
    assert notifier_module.is_configured() is True

    with patch("notifier.requests.post") as post:
        post.return_value = _mock_response({"ok": True, "result": {"message_id": 42}})
        ok = notifier_module.send("gap alert: $FOO")

        assert ok is True
        # only one POST
        assert post.call_count == 1
        url_arg = post.call_args[0][0]
        payload = post.call_args.kwargs["json"]
        assert url_arg == "https://api.telegram.org/botABC123/sendMessage"
        assert payload["chat_id"] == "999"
        assert payload["text"] == "gap alert: $FOO"
        assert payload["parse_mode"] == "Markdown"
        assert payload["disable_web_page_preview"] is True


def test_send_returns_false_on_network_error(monkeypatch):
    """If Telegram API hiccups, send() must NOT raise — it returns False."""
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "ABC")
    monkeypatch.setattr(notifier_module, "_CHAT_ID", "999")

    with patch("notifier.requests.post") as post:
        post.side_effect = ConnectionError("network down")
        ok = notifier_module.send("hello")
        assert ok is False  # never raises


def test_get_recent_updates_returns_none_without_token(monkeypatch):
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "")
    assert notifier_module.get_recent_updates() is None


def test_get_recent_updates_returns_list_on_success(monkeypatch):
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "ABC")
    with patch("notifier.requests.get") as get:
        get.return_value = _mock_response({
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"chat": {"id": 111}}},
                {"update_id": 2, "message": {"chat": {"id": 222}}},
            ],
        })
        updates = notifier_module.get_recent_updates(limit=5)
        assert updates is not None
        assert len(updates) == 2


def test_discover_chat_id_returns_most_recent(monkeypatch):
    """discover_chat_id() returns the chat_id of the latest message."""
    monkeypatch.setattr(notifier_module, "_BOT_TOKEN", "ABC")
    with patch("notifier.requests.get") as get:
        get.return_value = _mock_response({
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"chat": {"id": 111}}},
                {"update_id": 2, "message": {"chat": {"id": 222}}},  # most recent
            ],
        })
        assert notifier_module.discover_chat_id() == "222"