# gmgn_top_traders — gmgn.ai top trader scraper

Scrapes the top-trader panel from a gmgn.ai Solana token page using Scrapling
stealth mode + multi-provider proxy rotation.

## What it does

Given a Solana mint (`CA`), fetches `https://gmgn.ai/sol/token/{CA}` and
extracts wallet addresses + 30d buy/sell/volume/PnL where the page exposes
them. Output schema:

```json
{
  "ca": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "source": "gmgn_stealth | stub_fallback",
  "fetched_at": 1788473242,
  "n_traders": 12,
  "traders": [
    {
      "wallet_address": "...",
      "trader_tag": "smart-money | kol | ...",
      "buy_30d": 42,
      "sell_30d": 10,
      "pnl_30d": 1234.56,
      "volume_30d": 9876.54,
      "last_active_timestamp": 1788400000
    }
  ]
}
```

## Anti-bot strategy

gmgn is behind Cloudflare with a JS challenge interstitial. We use Scrapling's
`StealthyFetcher` which:

- Generates browser-like TLS fingerprints (curl_cffi under the hood)
- Sets realistic User-Agent / Accept / Sec-CH-UA headers
- Loads page like a real Chrome

When Cloudflare passes, the page renders in HTML form (Next.js
`__NEXT_DATA__` JSON dump + anchor tags with `/sol/address/<addr>`). We extract
both. When Cloudflare blocks (HTTP 403) **or** the page is JS-only and our
parser finds zero rows, we fall back to a stub mode — the script always
returns a valid `GMGNResult`, never raises.

## Choosing proxies

gmgn's Cloudflare fingerprinting burns naive datacenters within hours. Pick the
option that matches your budget + scale:

| Source                          | Cost         | Stealth vs gmgn | When to use                                                       |
|---------------------------------|--------------|-----------------|-------------------------------------------------------------------|
| **Free datacenter (proxybroker)** | $0           | Bad — usually 403 / burned in minutes | Quick smoke tests only. Not for production. |
| **Residential ($3/mo)** — e.g. **Webshare rotating residential** | ~$3/mo starter | Good — residential IPs aren't flagged at the rate datacenter is | **Recommended default.** Cheap, rotates automatically, survives most pages. |
| **Mobile (4G/LTE proxy pool)**  | $50–$300/mo  | Best — mobile carrier IPs are basically never blocked | When Webshare starts returning stub data on hot tokens, or for sustained monitoring of high-value mints. |
| **VPN tunnel (Tailscale exit node → helius RPC)** | $5/mo VPS + free Tailscale | Mixed — depends on VPS datacenter reputation | **Cheapest path that *sometimes* works.** Run `tailscale exit-node` on a fresh VPS, route the scraper through it. Useful when you already have a Helius RPC plan and want one extra egress IP. |

**Practical recipe for Jiro:**

1. Start with a Webshare residential plan (`$3/mo` for 5GB, plenty for the
   sniper cadence). Drop the URL into `proxies.txt` (one per line,
   `http://user:pass@host:port`).
2. If you see `verdict: cloudflare_blocked` on every proxy row in
   `--test-proxies` output, upgrade to mobile.
3. If you're cost-sensitive and only need a single egress IP, set
   `export PROXY_URL=http://user:pass@vps-tailscale-exit:port` and tunnel
   through your VPS. Single proxy is the fastest path to diagnose whether
   *your egress IP* is the problem or *gmgn's bot detection* is.

## Proxy sources (priority order)

The `ProxyProvider` looks for proxies in this order; first one wins:

1. **`PROXY_URL` env var** — single proxy, fastest path for testing.
   Example: `export PROXY_URL=http://user:pass@resi1.example.com:8080`
2. **`proxies.txt` file** — one URL per line, format
   `http://user:pass@host:port`. Defaults to the file in this directory.
3. **`proxybroker` rotation** — only if `proxybroker` is installed; pulls a
   small batch of public free HTTP proxies (silent skip otherwise).
4. **`direct`** — fallback when nothing else is configured.

## CLI

```bash
# Plain fetch (auto-detects proxy sources)
~/ruangkerja/jiro/venv/bin/python gmgn_top_traders.py EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v --timeout 30

# Skip proxies entirely (diagnose whether your IP is the problem)
~/ruangkerja/jiro/venv/bin/python gmgn_top_traders.py <CA> --no-proxy --diagnose

# Test every known proxy once and dump per-proxy status as JSON
~/ruangkerja/jiro/venv/bin/python gmgn_top_traders.py --test-proxies

# Full diagnostic: proxy matrix + structured summary
~/ruangkerja/jiro/venv/bin/python gmgn_top_traders.py <CA> --diagnose
```

Output goes to `output/<CA>_traders.json`.

## New env vars

| Var          | Default | Effect                                                         |
|--------------|---------|----------------------------------------------------------------|
| `PROXY_URL`  | (none)  | Single-proxy override. Wins over `proxies.txt` and `proxybroker`. |
| `GMGN_LOG_LEVEL` | `INFO` | `DEBUG` logs every retry/attempt; `WARNING` shows only the stub-reason line. |

## Python API

```python
from gmgn_top_traders import fetch_top_traders, ProxyProvider

# Auto-detect proxy sources (env / file / proxybroker / direct)
res = fetch_top_traders("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# Explicit provider (e.g. test a different proxies file)
provider = ProxyProvider(proxies_file=Path("/etc/jiro/proxies.txt"))
res = fetch_top_traders(ca, proxy_provider=provider, use_proxy=True)

# Diagnostic: skip proxies
res = fetch_top_traders(ca, use_proxy=False, allow_stub=False)
```

## Stub fallback contract

When `source == "stub_fallback"`, addresses are **synthetic** (deterministic
per CA for reproducibility) and volume/PnL numbers are random. The schema is
identical to real data so downstream consumers (e.g. `cabal_detector`) won't
crash, but the values are meaningless. Always check `source` before using
results for trading decisions.

The `note` field now reports a SPECIFIC reason, e.g.:

- `stub because: gmgn returned 200 but JS-only body (len=2143 bytes, expected >= 8000)`
- `stub because: 403 Cloudflare blocked all proxies (tried 3 attempt(s), statuses=[403, 403, 403])`
- `stub because: all attempts failed: <connection error>`

## Known limitations

- gmgn embeds many panels inside a hydrated SPA. When the server returns a
  shell page that defers all rendering to JS, `StealthyFetcher` (HTTP-only)
  won't see the trader rows. Future upgrade: swap to a full Playwright
  session (slow, ~5s/page) or call gmgn's internal API directly with a
  signed session.
- We cannot bypass a hard Cloudflare interstitial; if you hit one, the
  module logs it and returns stub data.
- Cloudflare fingerprinting can detect and burn datacenter IPs within hours.
  Use residential proxies for any sustained scraping.
- `--test-proxies` does a single fetch per proxy. Real sustained scraping
  should re-test every ~100 requests to catch proxy death.
