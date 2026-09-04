#!/usr/bin/env python3
"""
gmgn_top_traders.py — Jiro Sniper Net module.

Scrape the "top traders" / "top holders" / "trading activity" panel from a
gmgn.ai Solana token page using Scrapling stealth mode + multi-provider proxy
rotation. gmgn is heavily Cloudflare-walled; this module degrades gracefully
into a stub-fallback so it NEVER crashes the caller.

Usage:
    from gmgn_top_traders import fetch_top_traders, GMGNResult
    res = fetch_top_traders("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    for trader in res.traders:
        print(trader.wallet_address, trader.volume_usd_30d)

CLI:
    python gmgn_top_traders.py <CA> [--out PATH] [--no-proxy] [--diagnose]
    python gmgn_top_traders.py --test-proxies
    python gmgn_top_traders.py <CA> --diagnose

Sources / fallback chain:
    1. gmgn.ai stealth HTML scrape through proxy rotation (real data when reachable)
    2. Stub fallback (clearly labelled, deterministic-ish) when blocked

Proxy sources, in priority order (first one wins):
    1. PROXY_URL env var        — single proxy, fastest path for testing
    2. proxies.txt file         — one per line, http://user:pass@host:port
    3. proxybroker rotation     — public free lists, only if proxybroker installed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from scrapling.fetchers import StealthyFetcher
except Exception as _exc:  # pragma: no cover
    StealthyFetcher = None
    _IMPORT_ERROR = repr(_exc)
else:
    _IMPORT_ERROR = None

try:
    import proxybroker  # noqa: F401  -- presence check, used via ProxyProvider lazily
    _PROXYBROKER_AVAILABLE = True
except Exception:
    _PROXYBROKER_AVAILABLE = False

# ---------- Logging ----------

log = logging.getLogger("gmgn_top_traders")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(_h)
log.setLevel(os.environ.get("GMGN_LOG_LEVEL", "INFO").upper())

# ---------- Config ----------

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_PROXIES_FILE = SCRIPT_DIR / "proxies.txt"

GMGN_BASE = "https://gmgn.ai"
GMGN_TOKEN_URL = GMGN_BASE + "/sol/token/{ca}"
GMGN_DIAGNOSTIC_URL = GMGN_BASE + "/sol/token/USDC"  # canonical page used by --test-proxies
DEFAULT_TIMEOUT_S = 60
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.5

# How small can a successful page be? gmgn's Next.js shell is ~30-40 KB but the
# top-traders panel is hydrated JS only on many mints. If we get a 200 but
# parse zero trader rows AND the body is short, we call it "JS-only" instead
# of guessing it's Cloudflare.
JS_ONLY_BODY_THRESHOLD_BYTES = 8_000

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


# ---------- Schema ----------

@dataclass
class Trader:
    wallet_address: str
    trader_tag: Optional[str] = None
    buy_30d: Optional[int] = None
    sell_30d: Optional[int] = None
    pnl_30d: Optional[float] = None
    volume_30d: Optional[float] = None
    last_active_timestamp: Optional[int] = None


@dataclass
class GMGNResult:
    ca: str
    source: str  # 'gmgn_stealth' | 'stub_fallback' | 'gmgn_api'
    fetched_at: int
    traders: List[Trader] = field(default_factory=list)
    raw_status: Optional[int] = None
    note: str = ""


# ---------- Proxy providers ----------

@dataclass
class ProxyEntry:
    """One concrete proxy candidate. ``source`` tells us where it came from."""
    url: str
    source: str  # 'env' | 'file' | 'proxybroker' | 'direct'

    def host(self) -> str:
        # http://user:pass@host:port  ->  host:port (or just 'direct')
        if self.source == "direct":
            return "direct"
        m = re.match(r"^https?://(?:[^@]+@)?([^:/]+)(?::(\d+))?", self.url)
        return f"{m.group(1)}:{m.group(2)}" if m and m.group(2) else (m.group(1) if m else self.url)


class ProxyProvider:
    """Resolves which proxies are available, in priority order.

    Order is deterministic and explicit:
        1. PROXY_URL env var (single proxy; wins for fast testing)
        2. proxies.txt file  (one per line)
        3. proxybroker       (only if the package is installed — silent skip otherwise)
        + a synthetic 'direct' entry always available for `--no-proxy` runs.
    """

    def __init__(
        self,
        proxies_file: Optional[Path] = None,
        env_var: str = "PROXY_URL",
    ) -> None:
        self.proxies_file = proxies_file or DEFAULT_PROXIES_FILE
        self.env_var = env_var
        self._proxybroker_pool: Optional[List[str]] = None  # lazy

    # --- detection (cheap) ---

    def detected_sources(self) -> List[str]:
        """Returns the *names* of proxy sources we found. Used by --diagnose."""
        sources: List[str] = []
        if os.environ.get(self.env_var):
            sources.append("env")
        if self.proxies_file.exists() and self._read_file_lines():
            sources.append("file")
        if _PROXYBROKER_AVAILABLE:
            sources.append("proxybroker")
        if not sources:
            sources.append("direct")
        return sources

    # --- resolve ---

    def all(self, include_direct: bool = True) -> List[ProxyEntry]:
        """Every proxy we know about, plus optional 'direct' fallback."""
        out: List[ProxyEntry] = []

        env = os.environ.get(self.env_var)
        if env:
            out.append(ProxyEntry(url=env.strip(), source="env"))

        for line in self._read_file_lines():
            out.append(ProxyEntry(url=line, source="file"))

        pb = self._proxybroker_sample()
        for url in pb:
            out.append(ProxyEntry(url=url, source="proxybroker"))

        if include_direct and not out:
            out.append(ProxyEntry(url="direct://localhost", source="direct"))
        elif include_direct:
            # also keep direct as last-resort fallback so we still produce a result
            out.append(ProxyEntry(url="direct://localhost", source="direct"))

        return out

    def first(self) -> Optional[ProxyEntry]:
        all_ = self.all(include_direct=True)
        return all_[0] if all_ else None

    # --- internals ---

    def _read_file_lines(self) -> List[str]:
        if not self.proxies_file.exists():
            return []
        out: List[str] = []
        for line in self.proxies_file.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
        return out

    def _proxybroker_sample(self) -> List[str]:
        """Return up to N free proxies via proxybroker. Silent no-op if missing."""
        if not _PROXYBROKER_AVAILABLE:
            return []
        if self._proxybroker_pool is not None:
            return self._proxybroker_pool
        # proxybroker is async; in this short-lived CLI we just probe a tiny
        # set and cache whatever we managed to get. If nothing comes back
        # within a couple seconds we move on with what we have.
        proxies: List[str] = []
        try:
            import asyncio
            from proxybroker import Broker  # type: ignore

            async def _grab() -> List[str]:
                grabbed: List[str] = []
                async def save(p, *args):  # type: ignore
                    grabbed.append(f"http://{p.host}:{p.port}")
                broker = Broker([save])
                # find free HTTP proxies from public sources; tight timeouts.
                try:
                    await asyncio.wait_for(
                        broker.find(
                            types=["HTTP", "HTTPS"],
                            countries=["US", "DE", "NL", "SG"],
                            limit=5,
                        ),
                        timeout=8.0,
                    )
                finally:
                    await broker.stop()
                return grabbed

            proxies = asyncio.run(_grab())
        except Exception as exc:  # noqa: BLE001
            log.debug("proxybroker sample failed: %r", exc)
        self._proxybroker_pool = proxies
        return proxies


# ---------- Stub fallback (deterministic synthetic) ----------

def _fake_address(seed: str, idx: int) -> str:
    """Deterministic-ish Solana-style base58 address for stub fallback."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    rnd = random.Random(f"{seed}-{idx}")
    return "".join(rnd.choice(alphabet) for _ in range(44))


def _stub_traders(ca: str, n: int = 10) -> List[Trader]:
    now = int(time.time())
    out: List[Trader] = []
    for i in range(n):
        buys = random.Random(f"{ca}-{i}-b").randint(2, 80)
        sells = random.Random(f"{ca}-{i}-s").randint(0, buys)
        pnl = round(random.Random(f"{ca}-{i}-p").uniform(-5000.0, 8000.0), 2)
        vol = round(buys * random.Random(f"{ca}-{i}-v").uniform(50.0, 500.0), 2)
        out.append(
            Trader(
                wallet_address=_fake_address(ca, i),
                trader_tag=None,
                buy_30d=buys,
                sell_30d=sells,
                pnl_30d=pnl,
                volume_30d=vol,
                last_active_timestamp=now - random.Random(f"{ca}-{i}-t").randint(60, 86400 * 7),
            )
        )
    return out


# ---------- HTML parsing ----------

def _parse_traders_from_html(html: str) -> List[Trader]:
    """Best-effort extractor. gmgn's top-traders panel renders server-side for
    many token pages (Next.js __NEXT_DATA__) and inside hydrated JS for the
    rest. We extract both with broad selectors."""
    from scrapling.parser import Selector  # type: ignore
    page = Selector(html)
    out: List[Trader] = []
    seen: set[str] = set()

    def _push(addr: str, **kwargs):
        if not addr or addr in seen or len(addr) < 32 or len(addr) > 64:
            return
        seen.add(addr)
        out.append(Trader(wallet_address=addr, **kwargs))

    # 1) __NEXT_DATA__ JSON dump (most reliable when present)
    try:
        for script in page.css("script#__NEXT_DATA__::text").extract():
            try:
                data = json.loads(script)
            except Exception:
                continue
            queue = [data]
            while queue:
                node = queue.pop()
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k in ("address", "wallet_address", "wallet", "trader_address", "funder") and isinstance(v, str):
                            _push(v)
                        else:
                            queue.append(v)
                elif isinstance(node, list):
                    queue.extend(node)
    except Exception:
        pass

    # 2) Anchor tags with /sol/address/<addr> pattern
    for el in page.css('a[href*="/sol/address/"]'):
        href = el.attrib.get("href", "")
        if not href:
            continue
        try:
            tail = href.split("/sol/address/")[-1].split("?")[0].split("/")[0]
            _push(tail)
        except Exception:
            continue

    # 3) data-* attributes that look like addresses
    for sel in ('[data-address]', '[data-wallet]', '[data-trader-address]'):
        for el in page.css(sel):
            for attr in ("data-address", "data-wallet", "data-trader-address"):
                v = el.attrib.get(attr)
                if v:
                    _push(v)

    return out


# ---------- HTTP wrapper ----------

def _extract_body(response: Any) -> str:
    """Pull HTML body text out of a Scrapling `Response` regardless of which
    attribute the installed Scrapling version exposes.

    Scrapling has reshuffled its body attributes across versions:
      - 0.4.x: `html_content` (TextHandler), `body` (bytes)
      - older: `text`, `html`, `content`
    """
    if response is None:
        return ""
    # Preferred order: html_content (TextHandler) -> body bytes -> older names
    hc = getattr(response, "html_content", None)
    if hc:
        try:
            return str(hc)
        except Exception:  # noqa: BLE001
            pass
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)) and body:
        return body.decode("utf-8", errors="ignore")
    for name in ("text", "html", "content", "raw", "text_content"):
        v = getattr(response, name, None)
        if isinstance(v, (str, bytes, bytearray)) and v:
            return v.decode("utf-8", errors="ignore") if isinstance(v, (bytes, bytearray)) else v
    return ""


def scrapling_with_proxy(
    proxy_url: Optional[str],
    url: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    max_retries: int = MAX_RETRIES,
    backoff_base_s: float = BACKOFF_BASE_S,
    user_agent: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[int], Optional[str], Dict[str, Any]]:
    """Fetch `url` with optional proxy, retrying with exponential backoff.

    Returns (response, status, body_text, meta) where meta carries timing and
    proxy info. ``response`` may be None on hard failure. ``status`` is the
    last HTTP status observed (None if the request never completed).
    """
    if StealthyFetcher is None:
        return None, None, None, {"error": f"scrapling import failed: {_IMPORT_ERROR}"}

    meta: Dict[str, Any] = {
        "proxy": proxy_url or "direct",
        "attempts": 0,
        "elapsed_s": 0.0,
    }
    started = time.monotonic()
    response = None
    status: Optional[int] = None
    body_text: Optional[str] = None

    for attempt in range(max_retries):
        meta["attempts"] = attempt + 1
        try:
            kwargs: Dict[str, Any] = {"headless": True}
            if proxy_url and not proxy_url.startswith("direct://"):
                kwargs["proxy"] = proxy_url
            t0 = time.monotonic()
            response = StealthyFetcher.fetch(
                url,
                user_agent=user_agent or random.choice(USER_AGENTS),
                timeout=timeout_s * 1000,
                **kwargs,
            )
            meta["last_attempt_elapsed_s"] = round(time.monotonic() - t0, 3)
            status = getattr(response, "status", None)
            body_text = _extract_body(response)
            if status == 200 and body_text:
                meta["body_len"] = len(body_text)
                meta["elapsed_s"] = round(time.monotonic() - started, 3)
                return response, status, body_text, meta
        except Exception as e:  # noqa: BLE001
            meta["last_error"] = repr(e)
            log.debug("attempt %d failed: %r", attempt + 1, e)

        time.sleep(backoff_base_s * (2 ** attempt))

    meta["elapsed_s"] = round(time.monotonic() - started, 3)
    return response, status, body_text, meta


# ---------- Stub reason classifier ----------

def _classify_stub_reason(
    attempts: List[Dict[str, Any]],
    body_text: Optional[str],
    last_status: Optional[int],
    parser_row_count: int = 0,
) -> str:
    """Turn a wall of failed attempts into a SPECIFIC human-readable reason.

    Returns one of:
        - "stub because: gmgn returned 200 but JS-only body (len=X bytes, expected >= Y)"
        - "stub because: gmgn returned 200 but parser found 0 trader rows (body_len=X)"
        - "stub because: 403 Cloudflare blocked all proxies"
        - "stub because: <last error>"
    """
    statuses = [a.get("status") for a in attempts if a.get("status") is not None]
    body_len = len(body_text) if body_text else 0

    if 200 in statuses and body_len and body_len < JS_ONLY_BODY_THRESHOLD_BYTES:
        return (
            f"stub because: gmgn returned 200 but JS-only body "
            f"(len={body_len} bytes, expected >= {JS_ONLY_BODY_THRESHOLD_BYTES})"
        )

    cf_blocked = [s for s in statuses if s in (403, 429, 503)]
    if cf_blocked and len(cf_blocked) == len(statuses):
        # All attempts blocked by Cloudflare-style status
        return (
            f"stub because: {cf_blocked[0]} Cloudflare blocked all proxies "
            f"(tried {len(statuses)} attempt(s), statuses={statuses})"
        )

    if 200 in statuses and parser_row_count == 0 and body_len >= JS_ONLY_BODY_THRESHOLD_BYTES:
        # Big body but our parser found no trader rows — top-traders panel is
        # almost certainly inside a hydrated SPA bundle.
        return (
            f"stub because: gmgn returned 200 but parser found 0 trader rows "
            f"(body_len={body_len} bytes — top-traders panel likely JS-only)"
        )

    if not statuses:
        last_err = next((a.get("last_error") for a in reversed(attempts) if a.get("last_error")), "unknown")
        return f"stub because: all attempts failed: {last_err}"

    return (
        f"stub because: mixed failure (last_status={last_status}, "
        f"statuses={statuses}, body_len={body_len})"
    )


# ---------- Main fetcher ----------

def fetch_top_traders(
    ca: str,
    *,
    proxies_file: Optional[Path] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    allow_stub: bool = True,
    use_proxy: bool = True,
    proxy_provider: Optional[ProxyProvider] = None,
) -> GMGNResult:
    """Fetch top traders for a Solana mint. Always returns a GMGNResult,
    never raises. Falls back to stub data when gmgn is unreachable.

    Backwards compatible: existing callers that only pass (ca, proxies_file,
    timeout_s, allow_stub) keep working.
    """
    ca = ca.strip()
    fetched_at = int(time.time())
    url = GMGN_TOKEN_URL.format(ca=ca)

    provider = proxy_provider or ProxyProvider(proxies_file=proxies_file or DEFAULT_PROXIES_FILE)

    if StealthyFetcher is None:
        if allow_stub:
            return GMGNResult(
                ca=ca,
                source="stub_fallback",
                fetched_at=fetched_at,
                traders=_stub_traders(ca),
                raw_status=None,
                note=f"scrapling import failed: {_IMPORT_ERROR}",
            )
        raise RuntimeError(f"scrapling import failed: {_IMPORT_ERROR}")

    pool = provider.all() if use_proxy else [
        ProxyEntry(url="direct://localhost", source="direct")
    ]

    attempts_log: List[Dict[str, Any]] = []
    last_body: Optional[str] = None
    last_status: Optional[int] = None
    last_parser_rows: int = 0

    for entry in pool:
        proxy_url = None if entry.source == "direct" else entry.url
        resp, status, body_text, meta = scrapling_with_proxy(
            proxy_url=proxy_url,
            url=url,
            timeout_s=timeout_s,
        )
        meta["status"] = status
        attempts_log.append(meta)
        last_body = body_text
        last_status = status

        if status == 200 and body_text:
            traders = _parse_traders_from_html(body_text)
            last_parser_rows = len(traders)
            if traders:
                log.info(
                    "gmgn OK via proxy=%s attempts=%d body=%dB traders=%d",
                    entry.host(), meta["attempts"], meta.get("body_len", 0), len(traders),
                )
                return GMGNResult(
                    ca=ca,
                    source="gmgn_stealth",
                    fetched_at=fetched_at,
                    traders=traders[:20],
                    raw_status=status,
                    note=f"proxy={entry.host()} source={entry.source} attempt={meta['attempts']}",
                )
            log.info(
                "gmgn 200 via proxy=%s but no trader rows (body=%dB) — trying next proxy",
                entry.host(), len(body_text),
            )
            continue

        log.info(
            "gmgn attempt failed: proxy=%s status=%s err=%s",
            entry.host(), status, meta.get("last_error"),
        )

    # All proxies exhausted
    reason = _classify_stub_reason(attempts_log, last_body, last_status, parser_row_count=last_parser_rows)

    log.warning("%s — returning stub_fallback", reason)

    if allow_stub:
        return GMGNResult(
            ca=ca,
            source="stub_fallback",
            fetched_at=fetched_at,
            traders=_stub_traders(ca),
            raw_status=last_status,
            note=reason,
        )

    return GMGNResult(
        ca=ca,
        source="gmgn_stealth",
        fetched_at=fetched_at,
        traders=[],
        raw_status=last_status,
        note=reason,
    )


# ---------- Diagnostic helpers ----------

def test_proxies(
    *,
    proxies_file: Optional[Path] = None,
    target_url: str = GMGN_DIAGNOSTIC_URL,
    timeout_s: int = 30,
) -> List[Dict[str, Any]]:
    """Hit `target_url` once per known proxy. Returns one dict per proxy with
    status, elapsed time, body length, and whether any trader rows parsed.

    Output schema (also serialized as JSON to stdout in --test-proxies):
        {
          "proxy":  "resi1.example.com:8080" | "direct",
          "source": "env" | "file" | "proxybroker" | "direct",
          "url":    "http://user:pass@host:port" | "direct://localhost",
          "status": 200 | 403 | None,
          "elapsed_s": 1.234,
          "attempts": 1,
          "body_len": 42310,
          "traders_parsed": 0,
          "verdict": "ok_no_traders" | "ok_with_traders" | "cloudflare_blocked"
                      | "js_only" | "network_error",
          "error":  null | "<repr of exception>",
        }
    """
    provider = ProxyProvider(proxies_file=proxies_file or DEFAULT_PROXIES_FILE)
    pool = provider.all()
    results: List[Dict[str, Any]] = []

    for entry in pool:
        proxy_url = None if entry.source == "direct" else entry.url
        resp, status, body_text, meta = scrapling_with_proxy(
            proxy_url=proxy_url,
            url=target_url,
            timeout_s=timeout_s,
            max_retries=1,
        )
        body_len = len(body_text) if body_text else 0
        traders = _parse_traders_from_html(body_text) if body_text else []

        # verdict
        if status == 200 and body_len >= JS_ONLY_BODY_THRESHOLD_BYTES and traders:
            verdict = "ok_with_traders"
        elif status == 200 and body_len >= JS_ONLY_BODY_THRESHOLD_BYTES:
            verdict = "ok_no_traders"
        elif status == 200:
            verdict = "js_only"
        elif status in (403, 429, 503):
            verdict = "cloudflare_blocked"
        else:
            verdict = "network_error"

        results.append({
            "proxy": entry.host(),
            "source": entry.source,
            "url": entry.url,
            "status": status,
            "elapsed_s": meta.get("elapsed_s"),
            "attempts": meta.get("attempts"),
            "body_len": body_len,
            "traders_parsed": len(traders),
            "verdict": verdict,
            "error": meta.get("last_error"),
        })

    return results


# ---------- CLI ----------

def _save_json(res: GMGNResult, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ca": res.ca,
        "source": res.source,
        "fetched_at": res.fetched_at,
        "raw_status": res.raw_status,
        "note": res.note,
        "n_traders": len(res.traders),
        "traders": [asdict(t) for t in res.traders],
    }
    out_path.write_text(json.dumps(payload, indent=2))


def _print_table(records: List[Dict[str, Any]], columns: List[str]) -> None:
    """Tiny ASCII table printer so --test-proxies output is readable on TTY."""
    widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in records), default=0)) for c in columns}
    line = "  ".join(f"{c:<{widths[c]}}" for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    print(line)
    print(sep)
    for r in records:
        print("  ".join(f"{str(r.get(c, '')):<{widths[c]}}" for c in columns))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="gmgn.ai top traders scraper (stealth + multi-proxy + stub fallback)")
    parser.add_argument("ca", nargs="?", default=None,
                        help="Solana token mint address (omit when using --test-proxies)")
    parser.add_argument("--out", type=Path, default=None, help="output JSON path")
    parser.add_argument("--proxy", type=Path, default=DEFAULT_PROXIES_FILE, help="proxies.txt path")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--no-stub", action="store_true", help="fail instead of falling back to stub")
    parser.add_argument("--no-proxy", action="store_true",
                        help="skip proxy rotation and connect direct (diagnostic)")
    parser.add_argument("--test-proxies", action="store_true",
                        help="hit gmgn with each known proxy once and report per-proxy status as JSON")
    parser.add_argument("--diagnose", action="store_true",
                        help="run --test-proxies + a real fetch and print a structured summary")
    args = parser.parse_args(argv)

    # --test-proxies can run without a CA
    if args.test_proxies:
        results = test_proxies(proxies_file=args.proxy if args.proxy.exists() else None)
        print(json.dumps(results, indent=2, default=str))
        # Human-friendly summary table to stderr so it's separable from JSON
        print("\n# Summary table", file=sys.stderr)
        _print_table(results, ["proxy", "source", "status", "elapsed_s", "body_len",
                               "traders_parsed", "verdict"])
        return 0

    if not args.ca:
        parser.error("CA is required unless --test-proxies is set")

    if args.diagnose:
        # Step 1: proxy matrix
        print("# Proxy matrix", file=sys.stderr)
        results = test_proxies(proxies_file=args.proxy if args.proxy.exists() else None)
        print(json.dumps(results, indent=2, default=str), file=sys.stderr)
        _print_table(results, ["proxy", "source", "status", "elapsed_s", "body_len",
                               "traders_parsed", "verdict"])
        print("", file=sys.stderr)

        # Step 2: real fetch (with proxy unless --no-proxy)
        provider = ProxyProvider(proxies_file=args.proxy if args.proxy.exists() else None)
        detected = provider.detected_sources()
        print(f"# Detected proxy sources: {detected}", file=sys.stderr)

        res = fetch_top_traders(
            args.ca,
            proxies_file=args.proxy if args.proxy.exists() else None,
            timeout_s=args.timeout,
            allow_stub=not args.no_stub,
            use_proxy=not args.no_proxy,
            proxy_provider=provider,
        )

        out_path = args.out or (DEFAULT_OUTPUT_DIR / f"{args.ca}_traders.json")
        _save_json(res, out_path)

        summary = {
            "ca": res.ca,
            "source": res.source,
            "raw_status": res.raw_status,
            "n_traders": len(res.traders),
            "note": res.note,
            "out_path": str(out_path),
            "detected_proxy_sources": detected,
            "proxy_matrix": results,
        }
        print("\n# Structured diagnostic summary (JSON)", file=sys.stderr)
        print(json.dumps(summary, indent=2, default=str), file=sys.stderr)
        print(f"\nsource={res.source}  status={res.raw_status}  "
              f"traders={len(res.traders)}  -> {out_path}")
        if res.note:
            print(f"note: {res.note}")
        return 0

    # Plain fetch path
    provider = ProxyProvider(proxies_file=args.proxy if args.proxy.exists() else None)
    res = fetch_top_traders(
        args.ca,
        proxies_file=args.proxy if args.proxy.exists() else None,
        timeout_s=args.timeout,
        allow_stub=not args.no_stub,
        use_proxy=not args.no_proxy,
        proxy_provider=provider,
    )

    out_path = args.out or (DEFAULT_OUTPUT_DIR / f"{args.ca}_traders.json")
    _save_json(res, out_path)

    print(f"source={res.source}  status={res.raw_status}  traders={len(res.traders)}  -> {out_path}")
    if res.note:
        print(f"note: {res.note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
