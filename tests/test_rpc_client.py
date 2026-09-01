#!/usr/bin/env python3
"""
test_rpc_client.py — unit tests for rpc_client failover behavior.
"""

import time
from unittest.mock import patch, MagicMock

import pytest


def _mock_response(json_data=None, raise_=False):
    r = MagicMock()
    r.raise_for_status = MagicMock(side_effect=raise_ if raise_ else None)
    r.json = MagicMock(return_value=json_data or {})
    return r


def test_first_provider_succeeds():
    """If the primary provider responds cleanly, fallbacks are never touched."""
    import rpc_client
    rpc_client._default = rpc_client.RpcClient()  # fresh client
    # force the client to use only the one we control
    rpc_client._default._providers = [
        rpc_client._Provider("https://primary", 5.0),
        rpc_client._Provider("https://backup", 5.0),
    ]

    with patch.object(rpc_client._default._session, "post") as post:
        post.return_value = _mock_response({"result": {"slot": 42}})
        result = rpc_client.rpc_call("getSlot", [])
        assert result == {"slot": 42}
        assert post.call_count == 1
        # only the primary should have been called
        assert post.call_args[0][0] == "https://primary"


def test_failover_on_primary_failure():
    """Primary throws -> backup is tried -> backup succeeds."""
    import rpc_client
    rpc_client._default = rpc_client.RpcClient()
    rpc_client._default._providers = [
        rpc_client._Provider("https://primary", 5.0),
        rpc_client._Provider("https://backup", 5.0),
    ]

    with patch.object(rpc_client._default._session, "post") as post:
        # primary raises (timeout), backup returns a clean slot
        post.side_effect = [
            _mock_response(raise_=True),
            _mock_response({"result": {"slot": 99}}),
        ]
        result = rpc_client.rpc_call("getSlot", [])
        assert result == {"slot": 99}
        assert post.call_count == 2
        urls_called = [c[0][0] for c in post.call_args_list]
        assert urls_called == ["https://primary", "https://backup"]


def test_failover_on_jsonrpc_error():
    """Primary returns JSON-RPC error -> treat as failure, try next."""
    import rpc_client
    rpc_client._default = rpc_client.RpcClient()
    rpc_client._default._providers = [
        rpc_client._Provider("https://primary", 5.0),
        rpc_client._Provider("https://backup", 5.0),
    ]

    with patch.object(rpc_client._default._session, "post") as post:
        post.side_effect = [
            _mock_response({"error": {"code": -32600, "message": "bad"}}),
            _mock_response({"result": {"slot": 7}}),
        ]
        result = rpc_client.rpc_call("getSlot", [])
        assert result == {"slot": 7}
        assert post.call_count == 2


def test_returns_none_when_all_fail():
    """Every provider fails -> rpc_call returns None (caller skips cycle)."""
    import rpc_client
    rpc_client._default = rpc_client.RpcClient()
    rpc_client._default._providers = [
        rpc_client._Provider("https://primary", 5.0),
        rpc_client._Provider("https://backup", 5.0),
    ]

    with patch.object(rpc_client._default._session, "post") as post:
        post.side_effect = [
            _mock_response(raise_=True),
            _mock_response(raise_=True),
        ]
        result = rpc_client.rpc_call("getSlot", [])
        assert result is None


def test_provider_in_cooldown_is_skipped():
    """A provider that just failed should be skipped on the next call
    until its cooldown expires."""
    import rpc_client
    rpc_client._default = rpc_client.RpcClient()
    rpc_client._default._providers = [
        rpc_client._Provider("https://primary", 5.0),
        rpc_client._Provider("https://backup", 5.0),
    ]
    # artificially put primary in cooldown for 60s
    rpc_client._default._providers[0].cooldown_until = time.time() + 60

    with patch.object(rpc_client._default._session, "post") as post:
        post.return_value = _mock_response({"result": {"slot": 5}})
        rpc_client.rpc_call("getSlot", [])
        # primary was skipped — only backup got hit
        assert post.call_count == 1
        assert post.call_args[0][0] == "https://backup"


def test_providers_from_env(monkeypatch):
    """RPC_URL goes first, RPC_FALLBACK_URLS fills the rest, dedup."""
    monkeypatch.setenv("RPC_URL", "https://helius")
    monkeypatch.setenv("RPC_FALLBACK_URLS", "https://quicknode,https://helius,https://triton")
    import rpc_client
    importlib = __import__("importlib").reload(rpc_client)
    urls = rpc_client.providers_summary()
    # order matters: helius first, then unique fallbacks
    assert urls == "https://helius -> https://quicknode -> https://triton"