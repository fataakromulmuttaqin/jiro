#!/usr/bin/env python3
"""
chart_zone.py — Jiro data source adapter for chart.zone.

chart.zone is a multi-chain DEX screener / live charts terminal. Their
public data surfaces (all no-auth, free):

  /sitemaps/{chain}.xml — per-chain token index. Reveals:
    chart.zone/solana/{mint}  ← canonical token page
    chart.zone/base/{addr}
    chart.zone/bnb/{addr}
    chart.zone/{chain}/{addr}

  /markets/{chain}         — chain-wide trending list (server-rendered)
                              HTML with embedded JSON-LD ItemList of all
                              current trending tokens on that chain.

  /{chain}/{addr}            — token detail page. Server-rendered with
                              inline RSC payload (Next.js). Useful for
                              pulling price, liquidity, FDV, holder
                              counts, social links, etc.

The combination gives us: a chain index (sitemaps) + a trending list
(markets page) + per-token detail (item page). All free, all no-auth,
all real data.

Chains supported (seen in sitemaps): solana, base, bsc, robinhood,
eth, stable, arc, ink, hyperevm, tempo.

Usage:
    ./venv/bin/python chart_zone.py --list-tokens --chain solana --limit 20
    ./venv/bin/python chart_zone.py --token-detail <mint> --chain solana
    ./venv/bin/python chart_zone.py --sitemap solana
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Optional

API_BASE = "https://chart.zone"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

KNOWN_CHAINS = [
    "solana", "base", "bsc", "robinhood", "eth",
    "stable", "arc", "ink", "hyperevm", "tempo",
]


def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"__ERROR__:{e}"


def fetch_sitemap(chain: str) -> List[str]:
    """Return the list of token mints/addresses for a chain from the
    public sitemap. ~30 entries per chain."""
    if chain not in KNOWN_CHAINS:
        return []
    url = f"{API_BASE}/sitemaps/{chain}.xml"
    xml = _http_get(url)
    if xml.startswith("__ERROR__"):
        return []
    return re.findall(rf"<loc>{API_BASE}/{chain}/([^<]+)</loc>", xml)


def fetch_markets_page(chain: str) -> List[Dict[str, Any]]:
    """Fetch the chain's trending-token page. Extracts embedded Schema.org
    ItemList (position/name/url) and any mints the page mentions. The
    page also has an inline RSC payload (Next.js) but ItemList is the
    most reliable surface."""
    if chain not in KNOWN_CHAINS:
        return []
    url = f"{API_BASE}/markets/{chain}"
    html = _http_get(url)
    if html.startswith("__ERROR__"):
        return []
    out: List[Dict[str, Any]] = []
    # Schema.org ItemList — pretty-printed, easy to parse
    for m in re.finditer(
        r'\{"@type":"ListItem","position":(\d+),"name":"([^"]+)","url":"[^"]+/' + chain + r'/([^"/]+)"\}',
        html,
    ):
        out.append({
            "position": int(m.group(1)),
            "name": m.group(2),
            "address": m.group(3),
        })
    # Also pull any mint that appears in HTML body as a fallback
    if not out:
        mints = sorted(set(re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", html)))
        for i, m in enumerate(mints, 1):
            out.append({"position": i, "name": "?", "address": m})
    return out


def fetch_token_page(chain: str, address: str) -> Dict[str, Any]:
    """Fetch a single token's page. Returns the HTML plus extracted
    RSC payload (Next.js). Most fields are in __next_f.push() blocks;
    the data is JSON-string-escaped, so we strip one layer."""
    if chain not in KNOWN_CHAINS:
        return {"_error": f"unknown chain {chain}"}
    url = f"{API_BASE}/{chain}/{address}"
    html = _http_get(url)
    if html.startswith("__ERROR__"):
        return {"_error": html}
    out: Dict[str, Any] = {"chain": chain, "address": address, "url": url}
    # Title — usually "{SYMBOL} Price & Live {CHAIN} Chart | chart.zone"
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        out["title"] = m.group(1)
    # Description meta
    m = re.search(r'<meta name="description" content="([^"]+)"', html)
    if m:
        out["description"] = m.group(1)
    # Image / chart preview
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        out["og_image"] = m.group(1)
    # JSON-LD (Schema.org) — usually has token name/symbol/url
    for blk in re.findall(r'<script type="application/ld\+json">(.+?)</script>', html, re.DOTALL):
        try:
            data = json.loads(blk)
            if isinstance(data, dict) and "@type" in data:
                out.setdefault("schema", []).append(data)
        except Exception:
            pass
    # RSC payload — Next.js serializes the page tree as "1:..." chunks
    rsc_chunks = re.findall(r'self\.__next_f\.push\(\[1,"([^"]+)"\]\)', html, re.DOTALL)
    if rsc_chunks:
        out["rsc_chunk_count"] = len(rsc_chunks)
        # Concatenate and look for token-like fields
        joined = "".join(rsc_chunks)
        out["rsc_chars"] = len(joined)
        # Try to find quoted JSON objects within (Next.js inlines props)
        for prop in ("name", "symbol", "address", "decimals", "chain",
                     "imageUrl", "description", "telegram", "twitter",
                     "website", "price", "priceUsd", "liquidity", "fdv",
                     "marketCap", "volume24h", "holders", "holdersCount",
                     "totalSupply", "circulatingSupply", "txCount",
                     "creationTime", "creator", "dexId", "pairAddress"):
            for m in re.finditer(rf'"{prop}":(?:"([^"]{{1,200}}?)"|([0-9.]+))', joined):
                val = m.group(1) or m.group(2)
                if val and prop not in out:
                    out[prop] = val
    return out


def to_candidate(detail: Dict[str, Any], address: str, chain: str) -> Dict[str, Any]:
    """Reshape a chart.zone token detail into a Jiro candidate."""
    sym = detail.get("symbol") or (detail.get("name") or "?").split(" ")[0]
    return {
        "term": sym,
        "description": f"chart.zone {chain} {sym}  {detail.get('url','')}",
        "score": 0.0,
        "launch": {
            "mint": address,
            "pair_url": detail.get("url", ""),
        },
        "is_gap_candidate": True,
        "chain": chain,
        "address": address,
        "title": detail.get("title", ""),
        "og_image": detail.get("og_image", ""),
        "description": detail.get("description", ""),
        "source": "chart_zone",
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="chart.zone data adapter")
    ap.add_argument("--chain", default="solana", choices=KNOWN_CHAINS)
    ap.add_argument("--list-tokens", action="store_true", help="list trending on chain")
    ap.add_argument("--token-detail", metavar="ADDRESS", help="fetch one token page")
    ap.add_argument("--sitemap", action="store_true", help="dump sitemap mints only")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    if args.sitemap:
        mints = fetch_sitemap(args.chain)
        print(f"chart.zone /{args.chain} sitemap: {len(mints)} tokens")
        for m in mints[: args.limit]:
            print(f"  https://chart.zone/{args.chain}/{m}")
        return 0

    if args.token_detail:
        detail = fetch_token_page(args.chain, args.token_detail)
        if "_error" in detail:
            print(f"[!] {detail['_error']}")
            return 1
        for k, v in detail.items():
            if k == "schema":
                continue
            print(f"  {k}: {v}")
        return 0

    if args.list_tokens or True:
        items = fetch_markets_page(args.chain)
        print(f"chart.zone /markets/{args.chain}: {len(items)} items")
        if not items:
            return 1
        print(f"\n{'pos':<5} {'name':<32} {'address':<48} {'url'}")
        print("-" * 110)
        for it in items[: args.limit]:
            print(f"{it.get('position',0):<5} {it.get('name','?')[:30]:<32} "
                  f"{it.get('address','?'):<48} "
                  f"https://chart.zone/{args.chain}/{it.get('address','')}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
