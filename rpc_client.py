#!/usr/bin/env python3
"""
rpc_client.py — multi-provider Solana RPC client with automatic failover.

Why: public mainnet-beta is rate-limited and unreliable for any real use.
Helius is great but even paid tiers have occasional hiccups — a 3-provider
rotation keeps the bot from going dark just because one provider blinked.

Design:
- RPC_URLS is an ordered list (primary first). Each provider gets tried in
  order with its own per-request timeout.
- A provider that fails or times out gets a short cooldown (cooldown_s) before
  it can be retried, so we don't hammer a flapping endpoint.
- .rpc() returns the JSON `result` field on success, or None on total failure
  (every provider unavailable). Caller decides what to do with None — most
  should treat it as "skip this cycle".
- Health stats are tracked in-memory and printable via stats() so you can
  see which provider is actually carrying the load.

Env:
  RPC_URL=...           # primary (Helius). If set, goes first.
  RPC_FALLBACK_URLS=... # comma-separated fallbacks (QuickNode,Triton,...)
  RPC_TIMEOUT_S=8       # per-request timeout (default 8s)
  RPC_COOLDOWN_S=30     # how long to back off a failing provider (default 30s)
"""

import os
import time
import threading
from typing import Optional, List, Dict, Any

import requests


_DEFAULT_TIMEOUT_S = 8.0
_DEFAULT_COOLDOWN_S = 30.0
# last-resort if user set nothing — public mainnet-beta. Not great, but won't
# crash if someone forgets to configure RPC_URL.
_PUBLIC_FALLBACK = "https://api.mainnet-beta.solana.com"


class _Provider:
    __slots__ = ("url", "timeout_s", "cooldown_until", "fail_count", "ok_count")

    def __init__(self, url: str, timeout_s: float):
        self.url = url
        self.timeout_s = timeout_s
        self.cooldown_until: float = 0.0
        self.fail_count: int = 0
        self.ok_count: int = 0

    def is_available(self, now: float) -> bool:
        return now >= self.cooldown_until

    def mark_ok(self) -> None:
        self.ok_count += 1

    def mark_fail(self, cooldown_s: float, now: float) -> None:
        self.fail_count += 1
        self.cooldown_until = now + cooldown_s


def _build_provider_list() -> List[_Provider]:
    timeout_s = float(os.environ.get("RPC_TIMEOUT_S", _DEFAULT_TIMEOUT_S))

    urls: List[str] = []
    primary = os.environ.get("RPC_URL", "").strip()
    if primary:
        urls.append(primary)
    fallbacks_raw = os.environ.get("RPC_FALLBACK_URLS", "").strip()
    if fallbacks_raw:
        for u in fallbacks_raw.split(","):
            u = u.strip()
            if u and u not in urls:
                urls.append(u)
    if not urls:
        urls.append(_PUBLIC_FALLBACK)

    # de-dup while preserving order
    seen = set()
    deduped: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    return [_Provider(u, timeout_s) for u in deduped]


class RpcClient:
    def __init__(self):
        self._providers: List[_Provider] = _build_provider_list()
        self._lock = threading.Lock()
        self._session = requests.Session()

    def providers_summary(self) -> str:
        return " -> ".join(p.url for p in self._providers)

    def stats(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [
                {
                    "url": p.url,
                    "ok": p.ok_count,
                    "fail": p.fail_count,
                    "in_cooldown": not p.is_available(now),
                    "cooldown_remaining_s": max(0.0, p.cooldown_until - now),
                }
                for p in self._providers
            ]

    def rpc(self, method: str, params: list, timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Try providers in order until one returns successfully. Returns the
        `result` field of the first successful response, or None if every
        provider failed / is in cooldown."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        cooldown_s = float(os.environ.get("RPC_COOLDOWN_S", _DEFAULT_COOLDOWN_S))

        with self._lock:
            providers_snapshot = list(self._providers)

        for p in providers_snapshot:
            now = time.time()
            if not p.is_available(now):
                continue
            req_timeout = timeout_s if timeout_s is not None else p.timeout_s
            try:
                r = self._session.post(p.url, json=payload, timeout=req_timeout)
                r.raise_for_status()
                data = r.json()
                if "error" in data and data["error"]:
                    with self._lock:
                        p.mark_fail(cooldown_s, time.time())
                    continue
                with self._lock:
                    p.mark_ok()
                return data.get("result")
            except Exception:
                with self._lock:
                    p.mark_fail(cooldown_s, time.time())
                continue
        return None


# module-level singleton — most callers just want one shared client
_default = RpcClient()


def get_rpc_client() -> RpcClient:
    """Return the shared multi-provider RpcClient instance.

    Added so callers can `client = get_rpc_client(); client.rpc(...)`
    for explicit reuse. Older `rpc_call(method, params)` wrapper below
    stays for backwards compatibility.
    """
    return _default


def rpc_call(method: str, params: list, timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Backwards-compatible wrapper: rpc_call(method, params) -> result or None.
    Same semantics as the old single-RPC trading.rpc_call() helper, but now
    with failover built in."""
    return _default.rpc(method, params, timeout_s=timeout_s)


def providers_summary() -> str:
    return _default.providers_summary()


def stats() -> List[Dict[str, Any]]:
    return _default.stats()