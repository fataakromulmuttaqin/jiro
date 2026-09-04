#!/usr/bin/env python3
"""
top_traders_adapter.py — Jiro Sniper Net module.

Given a Solana token mint address, fetches top-trader context from public
APIs and emits a unified JSON list that `cabal_detector.py` can ingest.

SOURCES TRIED (in order):
1. DexScreener  — `https://api.dexscreener.com/latest/dex/tokens/{CA}`
                  Free, no auth, reliable. Returns AGGREGATED pair-level
                  buy/sell counts and 24h volume per DEX pair. NOT per-wallet.
                  We synthesize a "trader row" per pair so cabal_detector has
                  at least *some* liquidity/activity signal even when the
                  per-wallet sources are unreachable. The source label for
                  these rows is `dexscreener_pair` and the `wallet_address`
                  is the pair address (clearly labeled, never confused with
                  a real wallet).

2. GMGN         — `https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/{CA}
                    ?interval=1d&orderby=volume&direction=desc&limit=20`
                  Free, but Cloudflare-walled from many server IPs. When
                  reachable, returns real per-wallet ranked traders with
                  buy/sell counts and PnL. Source label: `gmgn`.

3. Birdeye      — `https://public-api.birdeye.so/defi/v2/tokens/top_traders`
                  Requires `BIRDEYE_API_KEY` env var. Real per-wallet ranked
                  traders with PnL. Source label: `birdeye`. Skipped cleanly
                  if no key.

4. On-chain RPC — When env points to a Solana RPC (jiro's `rpc_client`),
                  falls back to scanning recent tx signatures for the mint's
                  largest token accounts and extracting fee-payer wallets.
                  Free, slow. Source label: `onchain_rpc`.

OUTPUT:
  output/{CA}_traders.json      — unified merged list
  output/{CA}_raw_dexscreener.json
  output/{CA}_raw_gmgn.json
  output/{CA}_raw_birdeye.json
  output/{CA}_raw_onchain.json (only if on-chain path was exercised)
  output/{CA}_report.json      — full summary (sources hit, errors, counts)

INTEGRATION WITH CABAL_DETECTOR:
  cabal_seeds.json uses schema `{funder_address: "CabalName"}`. The wallets
  we discover here are *buyers*, not funders. `append_to_cabal_seeds()`
  therefore prefixes imported names with `auto-` and only adds wallets that
  appear in 2+ sources or have positive PnL (a stronger signal than just
  "showed up"). These seeds then contribute the +0.3 "known cabal" boost
  in cabal_detector when a cluster's shared_funder matches.

  For a more targeted path, hand the `wallets[]` list of this adapter's
  output straight to `profile_top_holders.py` to compute funder relations,
  then run `cabal_detector.analyze_report()`.

LIMITATIONS:
  - DexScreener is the ONLY source that works from a vanilla Linux server
    with zero config (tested from this VM). GMGN is Cloudflare-walled.
    Birdeye needs a paid/free key.
  - When ONLY DexScreener works, every row is pair-level — cabal_detector
    cannot use those as funder addresses. Use `append_to_cabal_seeds()`
    with `cabal_label=None` to keep the file clean or skip seeding.
  - Rate limits respected: 1s sleep between calls, exponential backoff
    on HTTP 429.

USAGE:
  python top_traders_adapter.py <MINT_CA> [--limit 20] [--no-merge]
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Module-level config
# ---------------------------------------------------------------------------

ADAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ADAPTER_DIR, "output")
SAMPLES_DIR = os.path.join(ADAPTER_DIR, "samples")

# Tunables
DEFAULT_LIMIT = 20          # how many traders to ask each per-wallet source for
INTER_REQUEST_SLEEP_S = 1.0 # polite pause between API calls
REQUEST_TIMEOUT_S = 12
MAX_RETRIES = 3             # retries on 429 / 5xx
BACKOFF_BASE_S = 2.0        # exponential backoff base (2s, 4s, 8s ...)

# Endpoints
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{ca}"
GMGN_SWAPS_URL = (
    "https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/{ca}"
    "?interval={interval}&orderby={orderby}&direction={direction}&limit={limit}"
)
BIRDEYE_TOP_TRADERS_URL = "https://public-api.birdeye.so/defi/v2/tokens/top_traders"

# Try several GMGN interval values — Cloudflare may rate-limit us off
# different ones depending on region.
_GMGN_INTERVAL_CANDIDATES = ("1d", "7d", "24h", "1h")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no `requests` so this works in zero-config envs)
# ---------------------------------------------------------------------------

class _FetchResult:
    __slots__ = ("ok", "status", "data", "error", "url")
    def __init__(self, ok: bool, status: int, data: Any, error: str, url: str):
        self.ok = ok
        self.status = status
        self.data = data
        self.error = error
        self.url = url

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "url": self.url,
            "data_preview": (
                f"<{len(json.dumps(self.data))} chars>"
                if self.data is not None else None
            ),
        }


def _http_get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> _FetchResult:
    """GET a URL, expect JSON body. Exponential backoff on 429/5xx.

    Returns _FetchResult; never raises. `data` is parsed JSON or raw text
    on JSON-parse failure.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    last_error = ""
    last_status = 0
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                raw = r.read()
                last_status = r.status
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except (ValueError, UnicodeDecodeError) as e:
                    return _FetchResult(
                        ok=False, status=r.status, data=None,
                        error=f"json_decode_failed: {e}", url=url,
                    )
                return _FetchResult(True, r.status, parsed, "", url)
        except urllib.error.HTTPError as e:
            last_status = e.code
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_error = f"HTTP {e.code}: {body[:200]}"
            # Backoff on 429 or 5xx; abort immediately on 401/403/404.
            if e.code in (401, 403, 404):
                return _FetchResult(False, e.code, None, last_error, url)
            if attempt < retries:
                time.sleep(BACKOFF_BASE_S ** attempt)
        except urllib.error.URLError as e:
            last_error = f"URLError: {e}"
            if attempt < retries:
                time.sleep(BACKOFF_BASE_S ** attempt)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(BACKOFF_BASE_S ** attempt)
    return _FetchResult(False, last_status, None, last_error, url)


# ---------------------------------------------------------------------------
# Source 1 — DexScreener (no auth, always works)
# ---------------------------------------------------------------------------

def fetch_dexscreener_pairs(ca: str) -> Tuple[List[Dict[str, Any]], _FetchResult]:
    """Fetch pair-level data for the mint from DexScreener.

    Returns (rows, fetch_result). Each row is a synthetic trader entry
    where `wallet_address` is the *pair address* (clearly labeled). We
    always return the rows — DexScreener is the one source that won't
    let us down — but we never pretend these are real wallets.
    """
    url = DEXSCREENER_TOKEN_URL.format(ca=ca)
    res = _http_get_json(url)
    rows: List[Dict[str, Any]] = []
    if not res.ok or not isinstance(res.data, dict):
        return rows, res
    pairs = res.data.get("pairs") or []
    for p in pairs:
        # Only Solana pairs (DexScreener supports EVM too)
        if (p.get("chainId") or "").lower() != "solana":
            continue
        txns = p.get("txns") or {}
        vol = p.get("volume") or {}
        liq = p.get("liquidity") or {}
        h24 = txns.get("h24") or {}
        rows.append({
            "wallet_address": p.get("pairAddress") or "",
            "source": "dexscreener_pair",
            "buy_count": int(h24.get("buys") or 0),
            "sell_count": int(h24.get("sells") or 0),
            "volume_usd": float(vol.get("h24") or 0.0),
            "pnl_usd": None,
            "first_seen": None,
            "last_active": None,
            "label": f"{p.get('dexId','?')}/{p.get('baseToken',{}).get('symbol','?')}-{p.get('quoteToken',{}).get('symbol','?')}",
            "extra": {
                "liquidity_usd": float(liq.get("usd") or 0.0),
                "fdv": p.get("fdv"),
                "price_change_h24_pct": (p.get("priceChange") or {}).get("h24"),
                "pair_created_at": p.get("pairCreatedAt"),
                "dex_id": p.get("dexId"),
                "pair_url": p.get("url"),
            },
        })
    return rows, res


# ---------------------------------------------------------------------------
# Source 2 — GMGN (best-effort, Cloudflare-walled from most server IPs)
# ---------------------------------------------------------------------------

def fetch_gmgn_traders(
    ca: str, *, limit: int = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, Any]], _FetchResult, Optional[_FetchResult]]:
    """Fetch per-wallet ranked traders from GMGN.

    Returns (rows, primary_fetch_result, raw_for_debug). rows is empty
    if GMGN is unreachable. Tries several `interval` values and returns
    the first successful response.

    NOTE: GMGN's Cloudflare policy blocks most datacenter IPs (403 even
    with browser headers). This function will fail gracefully and return
    rows=[].
    """
    primary: Optional[_FetchResult] = None
    for interval in _GMGN_INTERVAL_CANDIDATES:
        url = GMGN_SWAPS_URL.format(
            ca=ca, interval=interval,
            orderby="volume", direction="desc", limit=limit,
        )
        # Add Origin/Referer to look more browser-like — still doesn't
        # bypass Cloudflare for server IPs, but doesn't hurt to try.
        res = _http_get_json(
            url,
            headers={
                "Origin": "https://gmgn.ai",
                "Referer": f"https://gmgn.ai/sol/token/{ca}",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        primary = res
        if res.ok:
            rows = _parse_gmgn_rows(res.data, ca)
            if rows:
                return rows, res, res
        # else fall through to next interval candidate
        time.sleep(INTER_REQUEST_SLEEP_S)
    return [], primary if primary else _FetchResult(False, 0, None, "no_attempt", ""), primary


def _parse_gmgn_rows(payload: Any, ca: str) -> List[Dict[str, Any]]:
    """Tolerantly extract trader rows from a GMGN response.

    The response shape has shifted over time; we try the documented
    `data.rank` array first, then a few other shapes we've seen.
    """
    rows: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return rows
    data = payload.get("data")
    if not isinstance(data, dict):
        return rows
    candidates = [
        data.get("rank"),
        data.get("rows"),
        data.get("swaps"),
        data.get("items"),
    ]
    items: Optional[List[Any]] = None
    for c in candidates:
        if isinstance(c, list):
            items = c
            break
    if items is None and isinstance(data.get("rank"), dict):
        items = data["rank"].get("items")
    if not isinstance(items, list):
        return rows
    for it in items:
        if not isinstance(it, dict):
            continue
        wallet = (
            it.get("wallet_address")
            or it.get("wallet")
            or it.get("address")
            or it.get("maker")
            or it.get("trader_address")
        )
        if not wallet:
            continue
        rows.append({
            "wallet_address": str(wallet),
            "source": "gmgn",
            "buy_count": int(it.get("buy_count") or it.get("buy") or 0),
            "sell_count": int(it.get("sell_count") or it.get("sell") or 0),
            "volume_usd": float(
                it.get("volume_usd")
                or it.get("total_volume_usd")
                or it.get("volume")
                or 0
            ),
            "pnl_usd": (
                float(it["pnl_usd"]) if it.get("pnl_usd") is not None else None
            ),
            "first_seen": (
                int(it["first_trade_unix_time"]) if it.get("first_trade_unix_time") else None
            ),
            "last_active": (
                int(it["last_trade_unix_time"]) if it.get("last_trade_unix_time") else None
            ),
            "label": it.get("twitter_username") or it.get("name") or "",
            "extra": {k: v for k, v in it.items() if k not in (
                "wallet_address","wallet","address","maker","trader_address",
                "buy_count","buy","sell_count","sell","volume_usd",
                "total_volume_usd","volume","pnl_usd",
                "first_trade_unix_time","last_trade_unix_time",
                "twitter_username","name",
            )},
        })
    return rows


# ---------------------------------------------------------------------------
# Source 3 — Birdeye (optional, requires API key)
# ---------------------------------------------------------------------------

def fetch_birdeye_traders(
    ca: str, *, limit: int = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, Any]], _FetchResult]:
    """Fetch top traders from Birdeye. Requires BIRDEYE_API_KEY env var.

    Skipped cleanly if no key is set. Birdeye's docs at
    docs.birdeye.so/reference/get-defi-v2-tokens-top_traders specify
    `address`, `time_frame`, `sort_type`, `sort_by`, `offset`, `limit`.
    """
    api_key = os.environ.get("BIRDEYE_API_KEY", "").strip()
    if not api_key:
        return [], _FetchResult(
            ok=False, status=0, data=None,
            error="BIRDEYE_API_KEY env var not set; skipping Birdeye",
            url=BIRDEYE_TOP_TRADERS_URL,
        )
    q = urllib.parse.urlencode({
        "address": ca,
        "time_frame": "24h",
        "sort_type": "desc",
        "sort_by": "volume",
        "limit": limit,
        "offset": 0,
    })
    url = f"{BIRDEYE_TOP_TRADERS_URL}?{q}"
    res = _http_get_json(url, headers={"X-API-KEY": api_key, "x-chain": "solana"})
    rows: List[Dict[str, Any]] = []
    if res.ok and isinstance(res.data, dict) and res.data.get("success"):
        items = ((res.data.get("data") or {}).get("items") or [])
        for it in items:
            if not isinstance(it, dict):
                continue
            wallet = it.get("address") or it.get("wallet") or it.get("owner")
            if not wallet:
                continue
            rows.append({
                "wallet_address": str(wallet),
                "source": "birdeye",
                "buy_count": int(it.get("buy") or it.get("buy_count") or 0),
                "sell_count": int(it.get("sell") or it.get("sell_count") or 0),
                "volume_usd": float(it.get("volume") or it.get("total_volume_usd") or 0),
                "pnl_usd": (float(it["pnl"]) if it.get("pnl") is not None else None),
                "first_seen": None,
                "last_active": None,
                "label": "",
                "extra": {},
            })
    return rows, res


# ---------------------------------------------------------------------------
# Source 4 — On-chain RPC (free, slow, last-resort)
# ---------------------------------------------------------------------------

def fetch_onchain_traders(
    ca: str, *, max_holders: int = 10, max_sigs_per_holder: int = 20,
) -> Tuple[List[Dict[str, Any]], _FetchResult]:
    """Use jiro's rpc_client (or a plain RPC_URL env) to:

    1. Find the mint's largest token accounts via getTokenLargestAccounts.
    2. For each, resolve the owner wallet via getAccountInfo (parsed).
    3. getSignaturesForAddress(owner, limit=max_sigs_per_holder).
    4. Each signature's fee payer = a wallet that interacted with this
       token account. We emit one row per unique wallet.

    Free, slow. Returns rows=[] if no RPC is reachable or jiro's rpc_client
    isn't importable (this adapter is allowed to be used standalone).
    """
    debug = {
        "endpoint": "rpc_client.getTokenLargestAccounts + getSignaturesForAddress",
        "rpc_url": os.environ.get("RPC_URL", ""),
        "max_holders": max_holders,
        "max_sigs_per_holder": max_sigs_per_holder,
    }
    rpc = _try_import_rpc()
    rows: List[Dict[str, Any]] = []
    if rpc is None:
        return rows, _FetchResult(
            ok=False, status=0, data=debug,
            error="no Solana RPC available (RPC_URL env var unset and jiro.rpc_client not importable)",
            url="",
        )
    # 1. largest accounts
    largest = rpc.rpc("getTokenLargestAccounts", [ca]) if hasattr(rpc, "rpc") else rpc.rpc_call(
        "getTokenLargestAccounts", [ca],
    )
    if not isinstance(largest, dict) or not largest.get("value"):
        return rows, _FetchResult(
            ok=False, status=0, data=debug,
            error="getTokenLargestAccounts returned no accounts",
            url="",
        )
    accounts = largest["value"][:max_holders]
    seen_wallets: Dict[str, Dict[str, Any]] = {}
    for acc in accounts:
        ata = acc.get("address")
        if not ata:
            continue
        # 2. owner of this token account
        info = rpc.rpc("getAccountInfo", [ata, {"encoding": "jsonParsed"}]) if hasattr(rpc, "rpc") else rpc.rpc_call(
            "getAccountInfo", [ata, {"encoding": "jsonParsed"}],
        )
        owner = (((info or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("owner")
        if not owner:
            continue
        # 3. signatures for the owner
        sigs = rpc.rpc("getSignaturesForAddress", [owner, {"limit": max_sigs_per_holder}]) if hasattr(rpc, "rpc") else rpc.rpc_call(
            "getSignaturesForAddress", [owner, {"limit": max_sigs_per_holder}],
        )
        if not isinstance(sigs, list):
            continue
        first_seen: Optional[int] = None
        last_active: Optional[int] = None
        if sigs:
            # getSignaturesForAddress returns newest-first
            last_active = sigs[0].get("blockTime")
            first_seen = sigs[-1].get("blockTime")
        if owner in seen_wallets:
            # merge — extend first_seen / last_active
            r = seen_wallets[owner]
            if first_seen and (r["first_seen"] is None or first_seen < r["first_seen"]):
                r["first_seen"] = first_seen
            if last_active and (r["last_active"] is None or last_active > r["last_active"]):
                r["last_active"] = last_active
            r["extra"]["n_sigs_seen"] += len(sigs)
            continue
        seen_wallets[owner] = {
            "wallet_address": owner,
            "source": "onchain_rpc",
            "buy_count": None,
            "sell_count": None,
            "volume_usd": None,
            "pnl_usd": None,
            "first_seen": first_seen,
            "last_active": last_active,
            "label": "",
            "extra": {
                "top_token_account": ata,
                "ui_token_amount": (acc.get("uiTokenAmount") or {}).get("uiAmount"),
                "n_sigs_seen": len(sigs),
            },
        }
    rows = list(seen_wallets.values())
    return rows, _FetchResult(
        ok=True, status=200, data=debug,
        error="", url="rpc://getTokenLargestAccounts",
    )


def _try_import_rpc():
    """Try to import jiro's rpc_client (sibling project). Returns None if
    the user is running this adapter standalone and no RPC_URL is set."""
    try:
        import importlib.util
        import sys as _sys
        candidates = [
            os.path.abspath(os.path.join(ADAPTER_DIR, "..", "..", "rpc_client.py")),
            os.path.abspath(os.path.join(ADAPTER_DIR, "..", "..", "rpc_client.py")),
        ]
        for path in candidates:
            if os.path.exists(path):
                spec = importlib.util.spec_from_file_location("jiro_rpc", path)
                mod = importlib.util.module_from_spec(spec)
                _sys.modules["jiro_rpc"] = mod
                spec.loader.exec_module(mod)
                if hasattr(mod, "rpc_call"):
                    return mod
                if hasattr(mod, "RpcClient"):
                    return mod.RpcClient()
    except Exception:
        pass
    # Fallback: use a minimal stand-in client if RPC_URL is set
    if os.environ.get("RPC_URL", "").strip():
        return _MinimalRpc(os.environ["RPC_URL"].strip())
    return None


class _MinimalRpc:
    """Minimal RPC client for when jiro's rpc_client isn't on PYTHONPATH.

    Supports only the methods fetch_onchain_traders uses:
    getTokenLargestAccounts, getAccountInfo, getSignaturesForAddress.
    """
    def __init__(self, url: str):
        self.url = url

    def _post(self, method: str, params: list) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
            data = json.loads(r.read())
        return (data or {}).get("result")

    def rpc(self, method: str, params: list) -> Any:
        return self._post(method, params)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_traders(rows_per_source: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Merge per-source rows into a unified list keyed by wallet_address.

    DexScreener pair rows are kept as-is (they're not real wallets, so the
    address used as key is the pair address; we don't merge them with
    per-wallet rows). Per-wallet rows from GMGN/Birdeye/onchain_rpc are
    merged by wallet — first-seen-min, last-active-max, sources-list,
    buy/sell counts and volume summed.
    """
    pair_rows: List[Dict[str, Any]] = []
    wallet_rows_by_addr: Dict[str, Dict[str, Any]] = {}

    for source, rows in rows_per_source.items():
        for r in rows:
            r = dict(r)  # copy
            if source == "dexscreener_pair":
                pair_rows.append(r)
                continue
            addr = r.get("wallet_address")
            if not addr:
                continue
            if addr not in wallet_rows_by_addr:
                r["sources"] = [r.pop("source")]
                wallet_rows_by_addr[addr] = r
                continue
            existing = wallet_rows_by_addr[addr]
            existing["sources"].append(source)
            for f in ("buy_count", "sell_count", "volume_usd"):
                a, b = existing.get(f), r.get(f)
                if a is None and b is None:
                    continue
                existing[f] = (a or 0) + (b or 0)
            # pnl: prefer a non-null value, then add if both present
            if existing.get("pnl_usd") is None:
                existing["pnl_usd"] = r.get("pnl_usd")
            elif r.get("pnl_usd") is not None:
                existing["pnl_usd"] = (existing["pnl_usd"] or 0) + r["pnl_usd"]
            for f in ("first_seen", "last_active"):
                a, b = existing.get(f), r.get(f)
                if a is None and b is not None:
                    existing[f] = b
                elif b is not None:
                    existing[f] = min(a, b) if f == "first_seen" else max(a, b)
            # extra
            existing.setdefault("extra", {})
            existing["extra"].update(r.get("extra") or {})
            # label
            if not existing.get("label") and r.get("label"):
                existing["label"] = r["label"]

    # Stable ordering: per-wallet rows first, sorted by volume desc; then pairs
    wallet_rows = sorted(
        wallet_rows_by_addr.values(),
        key=lambda r: (r.get("volume_usd") or 0, r.get("buy_count") or 0),
        reverse=True,
    )
    pair_rows.sort(key=lambda r: r.get("volume_usd") or 0, reverse=True)
    return wallet_rows + pair_rows


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(SAMPLES_DIR, exist_ok=True)


def _save_json(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# cabal_seeds integration
# ---------------------------------------------------------------------------

def load_cabal_seeds(path: str) -> Dict[str, str]:
    """Load {funder_address: cabal_name} from a cabal_seeds.json file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                str(k): str(v)
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
                and not k.startswith("_")  # skip _comment / _examples
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def append_to_cabal_seeds(
    traders: List[Dict[str, Any]],
    output_path: str,
    *,
    min_sources: int = 2,
    require_pnl_positive: bool = True,
    cabal_label_prefix: str = "auto",
) -> Dict[str, str]:
    """Append new wallets discovered by this adapter to a cabal_seeds.json.

    Filters:
      - Source must NOT be `dexscreener_pair` (those aren't wallets).
      - Must appear in >= min_sources sources OR have a positive pnl_usd
        (if require_pnl_positive=True).
      - Wallet must not already exist in the file.

    Args:
      traders: merged output from `merge_traders()`.
      output_path: path to cabal_seeds.json (created if missing).
      min_sources: minimum number of distinct sources a wallet must
        appear in (default 2 — relaxes to 1 if require_pnl_positive
        is False).
      require_pnl_positive: if True, also accept single-source wallets
        that have a positive pnl_usd.

    Returns:
      Dict of new entries that were actually written.
    """
    existing = load_cabal_seeds(output_path)
    new_entries: Dict[str, str] = {}
    for t in traders:
        addr = t.get("wallet_address")
        if not addr:
            continue
        if t.get("source") == "dexscreener_pair":
            continue
        # `sources` is set by merge_traders(); for unmerged calls, fall back
        # to the single source field
        sources = t.get("sources") or [t.get("source")] if t.get("source") else []
        n_sources = len([s for s in sources if s])
        if n_sources < min_sources:
            if not (require_pnl_positive
                    and t.get("pnl_usd") is not None
                    and t["pnl_usd"] > 0):
                continue
        if addr in existing:
            continue
        label = f"{cabal_label_prefix}-{addr[:4]}-{addr[-4:]}"
        new_entries[addr] = label

    if not new_entries:
        return new_entries

    # Merge into existing file (preserves _comment / _examples keys)
    full: Dict[str, Any] = {}
    if os.path.exists(output_path):
        try:
            with open(output_path, "r") as f:
                full = json.load(f)
            if not isinstance(full, dict):
                full = {}
        except (OSError, json.JSONDecodeError):
            full = {}
    full.update(new_entries)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(full, f, indent=2, sort_keys=True)
    os.replace(tmp, output_path)
    return new_entries


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def fetch_top_traders(ca: str, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Run all enabled sources and return the unified output.

    The returned dict is the FULL report (sources hit, errors, merged
    traders). Callers wanting just the merged list should access
    result["traders"].
    """
    _ensure_dirs()

    sources_attempted: List[str] = []
    rows_per_source: Dict[str, List[Dict[str, Any]]] = {}
    fetch_log: Dict[str, Any] = {}

    # ---- 1. DexScreener (always) ----
    sources_attempted.append("dexscreener")
    ds_rows, ds_res = fetch_dexscreener_pairs(ca)
    rows_per_source["dexscreener_pair"] = ds_rows
    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_raw_dexscreener.json"),
               {"fetch": ds_res.to_dict(), "pairs": ds_res.data})
    fetch_log["dexscreener"] = {
        "ok": ds_res.ok,
        "status": ds_res.status,
        "error": ds_res.error,
        "n_rows": len(ds_rows),
    }
    time.sleep(INTER_REQUEST_SLEEP_S)

    # ---- 2. GMGN (best-effort) ----
    sources_attempted.append("gmgn")
    gm_rows, gm_res, gm_primary = fetch_gmgn_traders(ca, limit=limit)
    rows_per_source["gmgn"] = gm_rows
    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_raw_gmgn.json"),
               {"fetch": (gm_primary or gm_res).to_dict(),
                "data": (gm_primary or gm_res).data})
    fetch_log["gmgn"] = {
        "ok": gm_res.ok,
        "status": gm_res.status,
        "error": gm_res.error,
        "n_rows": len(gm_rows),
    }
    time.sleep(INTER_REQUEST_SLEEP_S)

    # ---- 3. Birdeye (only if key) ----
    sources_attempted.append("birdeye")
    bd_rows, bd_res = fetch_birdeye_traders(ca, limit=limit)
    rows_per_source["birdeye"] = bd_rows
    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_raw_birdeye.json"),
               {"fetch": bd_res.to_dict(), "data": bd_res.data})
    fetch_log["birdeye"] = {
        "ok": bd_res.ok,
        "status": bd_res.status,
        "error": bd_res.error,
        "skipped_reason": (
            "BIRDEYE_API_KEY not set" if "BIRDEYE_API_KEY" in (bd_res.error or "")
            else None
        ),
        "n_rows": len(bd_rows),
    }
    time.sleep(INTER_REQUEST_SLEEP_S)

    # ---- 4. On-chain RPC (best-effort) ----
    sources_attempted.append("onchain_rpc")
    oc_rows, oc_res = fetch_onchain_traders(ca)
    rows_per_source["onchain_rpc"] = oc_rows
    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_raw_onchain.json"),
               {"fetch": oc_res.to_dict(), "rows": oc_rows})
    fetch_log["onchain_rpc"] = {
        "ok": oc_res.ok,
        "status": oc_res.status,
        "error": oc_res.error,
        "n_rows": len(oc_rows),
    }

    merged = merge_traders(rows_per_source)

    report = {
        "ca": ca,
        "ts": int(time.time()),
        "sources_attempted": sources_attempted,
        "fetch_log": fetch_log,
        "n_unique_wallets": sum(1 for r in merged if r.get("source") != "dexscreener_pair"),
        "n_pair_rows": sum(1 for r in merged if r.get("source") == "dexscreener_pair"),
        "traders": merged,
    }

    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_traders.json"), merged)
    _save_json(os.path.join(OUTPUT_DIR, f"{ca}_report.json"), report)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Fetch top traders for a Solana mint and feed cabal_detector.",
    )
    p.add_argument("ca", help="Solana token mint address (CA)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"per-wallet source limit (default {DEFAULT_LIMIT})")
    p.add_argument("--cabal-seeds", default=None,
                   help="append discovered wallets to this cabal_seeds.json")
    p.add_argument("--no-cabal-label", action="store_true",
                   help="don't write cabal_seeds.json even if requested")
    p.add_argument("--min-sources", type=int, default=2,
                   help="min number of sources a wallet must appear in "
                        "before it lands in cabal_seeds (default 2)")
    p.add_argument("--allow-single-source-pnl-positive", action="store_true",
                   default=True,
                   help="also seed wallets that appear in only one source "
                        "if they have positive pnl_usd (default true)")
    args = p.parse_args(argv)

    print(f"[top_traders_adapter] fetching traders for {args.ca}", file=sys.stderr)
    report = fetch_top_traders(args.ca, limit=args.limit)

    n_w = report["n_unique_wallets"]
    n_p = report["n_pair_rows"]
    print(
        f"[top_traders_adapter] done: {n_w} wallets, {n_p} pair rows, "
        f"report at output/{args.ca}_report.json",
        file=sys.stderr,
    )

    if args.cabal_seeds and not args.no_cabal_label:
        added = append_to_cabal_seeds(
            report["traders"],
            args.cabal_seeds,
            min_sources=args.min_sources,
            require_pnl_positive=args.allow_single_source_pnl_positive,
        )
        if added:
            print(
                f"[top_traders_adapter] seeded {len(added)} wallets into "
                f"{args.cabal_seeds}",
                file=sys.stderr,
            )
        else:
            print(
                f"[top_traders_adapter] no new wallets qualified for "
                f"{args.cabal_seeds}",
                file=sys.stderr,
            )

    # Always emit the merged JSON to stdout for piping
    print(json.dumps(report["traders"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))