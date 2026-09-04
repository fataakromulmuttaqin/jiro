#!/usr/bin/env python3
"""
smart_wallet_sync.py — Jiro Sniper Net module.

Wires the top-trader data sources (api_adapter, gmgn_scraper, on-chain) into
the user's `cabal_seeds.json`, with **TTL-based expiration** so that
rotated/abandoned wallets get pruned automatically.

Why TTL?
---------
A wallet that has been "exposed" by appearing in a public top-traders panel
will be drained and abandoned by its operator within days. Keeping it in the
seed DB past its useful life wastes the +0.3 "known cabal" boost on a ghost
address — false positives. So every entry carries `last_active_ts` and
`still_holding`; if `last_active_ts` is older than `--ttl-days` AND
`still_holding` is False, the entry is expired.

WHAT IT DOES
------------
1. Walks the output directories of each top-trader source:
     - api_adapter/output/         — `{CA}_traders.json` (DexScreener + GMGN + Birdeye + onchain_rpc merged)
     - gmgn_scraper/output/        — `{CA}_traders.json` (gmgn.ai stealth or stub fallback)
     - onchain/output/             — `{CA}_traders.json` (on-chain via Helius — if present)

2. Schema-merges the per-row dicts into a unified row:
     {wallet_address, sources[], buy_count, sell_count, realized_pnl_sol,
      volume_usd, last_active_ts, still_holding, discovered_ts}

3. Deduplicates by wallet_address. A wallet that appears in 2+ sources is
   tagged "high_confidence" and labelled "{CabalNameHint}-{short_addr}".
   A wallet seen by only one source is "single-source" and labelled
   "auto-{source}-{short_addr}".

4. Loads the existing `cabal_seeds.json` (flat `{address: "CabalName"}`),
   and a sidecar `cabal_seeds.meta.json` (TTL/hydration info —
   `_meta` does NOT live inside cabal_seeds.json because
   `cabal_detector.load_seed_cabals()` does `str(k): str(v)` and would
   silently coerce a nested `_meta` dict into a string label,
   corrupting the seeds).

5. **Migration**: any seed entry that has no entry in the sidecar meta
   file gets auto-bootstrapped with
   `_meta = {discovered_ts: today, last_active_ts: today, still_holding: True}`.
   The user manually curated it → we assume it's still good.

6. **TTL expiration**:
     - For each seed with meta, if `last_active_ts` is older than
       `--ttl-days` AND `not still_holding`, mark for expiry.
     - On `--dry-run`: print a kept/expired/added table and exit.
     - On real run: append expired entries to `cabal_seeds.expired.json`
       (append-only audit log) and remove them from `cabal_seeds.json`.
       Add new entries with their generated labels.

7. Writes a sync report at
   `~/ruangkerja/jiro/top_traders/sync/output/sync_{YYYY-MM-DD}.json`:
     {added[], expired[], kept[], by_source_counts: {<source>: <int>}, ...}

8. Sends a Telegram notification via `notifier.send()` ONLY when
   something was added or expired — not on no-op syncs. (Dry-run
   never sends, even when it would have otherwise.)

USAGE
-----
    python smart_wallet_sync.py                              # all sources, default 14-day TTL, real run
    python smart_wallet_sync.py --dry-run                    # preview only
    python smart_wallet_sync.py --source api_adapter         # single source
    python smart_wallet_sync.py --ttl-days 7 --dry-run
    python smart_wallet_sync.py --seed-from-cabal-detector /path/to/sniper_net_batch.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
TOP_TRADERS_DIR = SCRIPT_DIR.parent
JIRO_ROOT = TOP_TRADERS_DIR.parent

# Where each source writes its `_traders.json` outputs.
API_ADAPTER_DIR = TOP_TRADERS_DIR / "api_adapter"
API_ADAPTER_OUTPUT = API_ADAPTER_DIR / "output"

GMGN_SCRAPER_DIR = TOP_TRADERS_DIR / "gmgn_scraper"
GMGN_SCRAPER_OUTPUT = GMGN_SCRAPER_DIR / "output"

ONCHAIN_DIR = TOP_TRADERS_DIR / "onchain"  # may not exist yet
ONCHAIN_OUTPUT = ONCHAIN_DIR / "output"

CABAL_SEEDS_PATH = JIRO_ROOT / "cabal_seeds.json"
CABAL_SEEDS_EXAMPLE = JIRO_ROOT / "cabal_seeds.example.json"
CABAL_SEEDS_META = JIRO_ROOT / "cabal_seeds.meta.json"
CABAL_SEEDS_EXPIRED = JIRO_ROOT / "cabal_seeds.expired.json"

OUTPUT_DIR = SCRIPT_DIR / "output"

# Source identifiers — must match what the per-source scripts use as
# `source` values. We also tolerate pair-level rows from dexscreener
# (which are not real wallets — we keep them but tag clearly).
SOURCE_API_ADAPTER = "api_adapter"
SOURCE_GMGN_SCRAPER = "gmgn_scraper"
SOURCE_ONCHAIN = "onchain"

ALL_SOURCES = (SOURCE_API_ADAPTER, SOURCE_GMGN_SCRAPER, SOURCE_ONCHAIN)

# Confidence tiers
HIGH_CONFIDENCE = "high_confidence"  # seen in 2+ sources
SINGLE_SOURCE = "single_source"      # only one source


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class UnifiedRow:
    """A wallet, aggregated across N sources."""

    wallet_address: str
    sources: List[str] = field(default_factory=list)  # each occurrence is one source
    by_source: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    buy_count: int = 0
    sell_count: int = 0
    realized_pnl_sol: Optional[float] = None  # None when no source gave it
    unrealized_pnl_estimate: float = 0.0  # marked-to-market for held positions (onchain source)
    volume_usd: float = 0.0
    last_active_ts: int = 0  # most recent across sources
    still_holding: bool = False
    discovered_ts: int = field(default_factory=lambda: int(time.time()))
    confidence: str = SINGLE_SOURCE
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Loaders — one per source
# ---------------------------------------------------------------------------


def _load_traders_from_dir(out_dir: Path, source_id: str) -> List[Dict[str, Any]]:
    """Load all `{CA}_traders.json` files in a source's output dir.

    Each row is tagged with `_source = source_id` so the caller knows where
    it came from (the upstream file may not include this).
    """
    if not out_dir.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(out_dir.glob("*_traders.json")):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[sync] WARN: failed to read {path}: {e}", file=sys.stderr)
            continue

        # Two shapes: list (most sources) OR dict with `traders` key (gmgn_scraper, api_adapter report)
        if isinstance(data, dict):
            rows_raw = data.get("traders") or []
        elif isinstance(data, list):
            rows_raw = data
        else:
            continue

        for row in rows_raw:
            if not isinstance(row, dict):
                continue
            wallet = row.get("wallet_address") or row.get("wallet") or ""
            if not wallet:
                continue
            tagged = dict(row)
            tagged["_source"] = source_id
            tagged["_source_file"] = str(path.name)
            rows.append(tagged)
    return rows


def _load_all_sources(enabled: Iterable[str]) -> List[Dict[str, Any]]:
    """Load and merge raw rows from all enabled sources."""
    enabled_set = set(enabled)
    all_rows: List[Dict[str, Any]] = []
    if SOURCE_API_ADAPTER in enabled_set:
        all_rows.extend(_load_traders_from_dir(API_ADAPTER_OUTPUT, SOURCE_API_ADAPTER))
    if SOURCE_GMGN_SCRAPER in enabled_set:
        all_rows.extend(_load_traders_from_dir(GMGN_SCRAPER_OUTPUT, SOURCE_GMGN_SCRAPER))
    if SOURCE_ONCHAIN in enabled_set:
        all_rows.extend(_load_traders_from_dir(ONCHAIN_OUTPUT, SOURCE_ONCHAIN))
    return all_rows


# ---------------------------------------------------------------------------
# Schema-merge: raw rows → UnifiedRow
# ---------------------------------------------------------------------------


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _short_addr(addr: str) -> str:
    """Stable short label fragment for a wallet: first 4 + last 4."""
    if len(addr) >= 8:
        return f"{addr[:4]}-{addr[-4:]}"
    return addr[:8]


def _merge_rows(raw_rows: List[Dict[str, Any]]) -> Dict[str, UnifiedRow]:
    """Aggregate raw rows (across all sources) into one UnifiedRow per wallet.

    Per-source dicts are kept under `by_source[source_id]` so we can show
    provenance and never lose the original numbers.
    """
    out: Dict[str, UnifiedRow] = {}
    for row in raw_rows:
        wallet = row.get("wallet_address") or row.get("wallet") or ""
        if not wallet:
            continue
        src = row.get("_source") or "unknown"

        # Parse per-source numerics. Field names vary by source:
        #   api_adapter : buy_count, sell_count, pnl_usd, volume_usd
        #   gmgn_scraper: buy_30d,   sell_30d,   pnl_30d, volume_30d
        #   onchain     : buys,      sells,      realized_pnl_sol (already SOL),
        #                 buy_volume_sol / sell_volume_sol, still_holding bool
        buy = _safe_int(row.get("buy_count") or row.get("buy_30d") or row.get("buys"))
        sell = _safe_int(row.get("sell_count") or row.get("sell_30d") or row.get("sells"))

        # PnL: prefer already-SOL value if present (onchain); else convert USD→SOL.
        pnl_sol_direct = row.get("realized_pnl_sol")
        if pnl_sol_direct is not None:
            pnl_sol = _safe_float(pnl_sol_direct)
        else:
            pnl_usd = row.get("pnl_usd")
            pnl_30d = row.get("pnl_30d")
            pnl_picked = pnl_usd if pnl_usd is not None else pnl_30d
            sol_price = _safe_float(os.environ.get("SOL_PRICE_USD"), 200.0)
            pnl_sol = (_safe_float(pnl_picked) / sol_price) if pnl_picked is not None else None

        # Volume USD: prefer USD fields; onchain gives SOL, convert.
        vol_usd = _safe_float(row.get("volume_usd") or row.get("volume_30d"))
        if not vol_usd:
            buy_sol = _safe_float(row.get("buy_volume_sol"))
            sell_sol = _safe_float(row.get("sell_volume_sol"))
            onchain_vol_sol = buy_sol + sell_sol
            if onchain_vol_sol:
                sol_price = _safe_float(os.environ.get("SOL_PRICE_USD"), 200.0)
                vol_usd = onchain_vol_sol * sol_price

        # Unrealized PnL estimate: only the onchain source provides this
        # (position marked at avg cost basis). We accumulate the max across
        # sources in case a future source exposes it too.
        unrealized = _safe_float(row.get("unrealized_pnl_estimate") or row.get("unrealized_pnl_sol"))

        last_active = _safe_int(
            row.get("last_active_ts")
            or row.get("last_active_timestamp")
            or row.get("last_active")
            or row.get("first_seen_ts")
        )
        # DexScreener pair-level rows have last_active=None — fall back
        # to pair_created_at (ms) or 0.
        if not last_active and row.get("extra", {}).get("pair_created_at"):
            ms = _safe_int(row["extra"]["pair_created_at"])
            last_active = ms // 1000 if ms else 0
        if not last_active:
            # Pair-level rows may have nothing useful — use file mtime as
            # a weak "we saw it now" signal so they aren't immortal.
            try:
                fp = row.get("_source_file")
                if fp:
                    full_path = Path(fp)
                    if not full_path.is_absolute():
                        # Resolve relative to its source dir
                        if src == SOURCE_API_ADAPTER:
                            full_path = API_ADAPTER_OUTPUT / fp
                        elif src == SOURCE_GMGN_SCRAPER:
                            full_path = GMGN_SCRAPER_OUTPUT / fp
                        elif src == SOURCE_ONCHAIN:
                            full_path = ONCHAIN_OUTPUT / fp
                    if full_path.exists():
                        last_active = int(full_path.stat().st_mtime)
            except Exception:
                pass

        # still_holding: prefer upstream's explicit bool; else infer from
        # buy vs sell counts.
        still_holding = False
        if isinstance(row.get("still_holding"), bool):
            still_holding = row["still_holding"]
        elif buy > sell:
            still_holding = True

        u = out.get(wallet)
        if u is None:
            u = UnifiedRow(wallet_address=wallet, discovered_ts=int(time.time()))
            out[wallet] = u

        # Most-recent-wins per field; per-source provenance preserved.
        if src not in u.by_source:
            u.by_source[src] = {
                "buy_count": buy,
                "sell_count": sell,
                "pnl_sol": pnl_sol,
                "unrealized_pnl_estimate": unrealized,
                "volume_usd": vol_usd,
                "last_active_ts": last_active,
                "still_holding": still_holding,
            }
            u.sources.append(src)
        else:
            # Same source seen twice (e.g. multiple CAs): keep the
            # newest observation.
            existing = u.by_source[src]
            for k, k_alt in (("buy_count", "buys"), ("sell_count", "sells"), ("volume_usd", None)):
                # accept both the canonical name and the onchain alt name
                candidates = [row.get(k)]
                if k_alt:
                    candidates.append(row.get(k_alt))
                v = max((_safe_int(c) for c in candidates if c is not None), default=0)
                if v > existing.get(k, 0):
                    existing[k] = v
            # PnL: max of the numeric interpretations of both rows.
            new_pnl = pnl_sol if pnl_sol is not None else -1e18
            old_pnl = existing.get("pnl_sol")
            if old_pnl is None or new_pnl > old_pnl:
                existing["pnl_sol"] = pnl_sol
            if last_active > existing.get("last_active_ts", 0):
                existing["last_active_ts"] = last_active

        # Aggregate totals (sum across sources) + max(last_active) + OR(still_holding).
        u.buy_count = sum(v.get("buy_count", 0) for v in u.by_source.values())
        u.sell_count = sum(v.get("sell_count", 0) for v in u.by_source.values())
        u.realized_pnl_sol = None
        pnls = [v.get("pnl_sol") for v in u.by_source.values() if v.get("pnl_sol") is not None]
        if pnls:
            # Conservative: only set when ALL sources agree on sign OR
            # we have just one.
            if len(pnls) == 1:
                u.realized_pnl_sol = pnls[0]
            else:
                # mean — pnls has already been filtered for non-None above
                numeric = [float(p) for p in pnls if p is not None]
                u.realized_pnl_sol = sum(numeric) / len(numeric) if numeric else None
        # Unrealized PnL: take the max across sources (most optimistic signal —
        # useful for holders. If two sources disagree, prefer the higher one
        # because a wallet that hasn't exited has more upside potential).
        unrealizeds = [v.get("unrealized_pnl_estimate") for v in u.by_source.values() if v.get("unrealized_pnl_estimate") is not None]
        u.unrealized_pnl_estimate = max([x for x in unrealizeds if x is not None], default=0.0)
        u.volume_usd = sum(v.get("volume_usd", 0.0) for v in u.by_source.values())
        u.last_active_ts = max(
            (v.get("last_active_ts", 0) for v in u.by_source.values()),
            default=0,
        )
        u.still_holding = any(v.get("still_holding", False) for v in u.by_source.values())

    # Confidence tier
    for u in out.values():
        if len(u.sources) >= 2:
            u.confidence = HIGH_CONFIDENCE
        else:
            u.confidence = SINGLE_SOURCE

    return out


# ---------------------------------------------------------------------------
# cabal_seeds.json load/save + sidecar meta
# ---------------------------------------------------------------------------


def load_seeds_and_meta(write_ok: bool = True) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Load the cabal_seeds.json (flat) and the sidecar meta file.

    If cabal_seeds.json is missing and write_ok is True, seed it from
    cabal_seeds.example.json so the user always has something to start
    from. (Dry-runs pass write_ok=False to keep the filesystem clean.)

    If the sidecar is missing or has no entry for a known seed,
    auto-migrate.
    """
    if not CABAL_SEEDS_PATH.exists():
        if CABAL_SEEDS_EXAMPLE.exists() and write_ok:
            # Cold-start: copy the example so user can edit it.
            try:
                with open(CABAL_SEEDS_EXAMPLE, "r") as f:
                    seeds = json.load(f)
                if not isinstance(seeds, dict):
                    seeds = {}
                # Strip example-file sentinel keys ("_comment", "_examples")
                # so cabal_seeds.json stays a pure flat {addr: "CabalName"}
                # that cabal_detector.load_seed_cabals() can ingest without
                # any string-coercion shenanigans.
                sentinel_keys = [k for k in seeds if k.startswith("_")]
                for k in sentinel_keys:
                    seeds.pop(k, None)
                with open(CABAL_SEEDS_PATH, "w") as f:
                    json.dump(seeds, f, indent=2)
                print(
                    f"[sync] seeded {CABAL_SEEDS_PATH} from example ({len(seeds)} entries)",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"[sync] WARN: failed to bootstrap cabal_seeds.json: {e}", file=sys.stderr)
                seeds = {}
        else:
            # Either no example, or dry-run: just read from the example
            # in-memory if it exists, so we have a stable starting point.
            seeds = {}
            if CABAL_SEEDS_EXAMPLE.exists():
                try:
                    with open(CABAL_SEEDS_EXAMPLE, "r") as f:
                        seeds = json.load(f)
                    if not isinstance(seeds, dict):
                        seeds = {}
                    sentinel_keys = [k for k in seeds if k.startswith("_")]
                    for k in sentinel_keys:
                        seeds.pop(k, None)
                    if not write_ok:
                        print(
                            f"[sync] DRY-RUN: would have seeded {CABAL_SEEDS_PATH.name} "
                            f"from example ({len(seeds)} entries)",
                            file=sys.stderr,
                        )
                except Exception:
                    seeds = {}

    try:
        with open(CABAL_SEEDS_PATH, "r") as f:
            seeds = json.load(f)
        if not isinstance(seeds, dict):
            seeds = {}
    except (OSError, json.JSONDecodeError):
        seeds = {}

    # Drop sentinel keys from any pre-existing seeds file too
    sentinel_keys = [k for k in seeds if k.startswith("_")]
    for k in sentinel_keys:
        seeds.pop(k, None)

    # Sidecar meta
    meta: Dict[str, Dict[str, Any]] = {}
    if CABAL_SEEDS_META.exists():
        try:
            with open(CABAL_SEEDS_META, "r") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                meta = {}
            # Drop sentinel meta entries
            for k in [k for k in meta if k.startswith("_")]:
                meta.pop(k, None)
        except (OSError, json.JSONDecodeError):
            meta = {}

    return seeds, meta


def migrate_meta(seeds: Dict[str, str], meta: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Auto-create meta entries for seed addresses that don't have one.

    User-curated entries default to:
        {discovered_ts: today, last_active_ts: today, still_holding: True}
    """
    today = int(time.time())
    changed = False
    for addr in seeds.keys():
        if addr not in meta:
            meta[addr] = {
                "discovered_ts": today,
                "last_active_ts": today,
                "still_holding": True,
                "source": "manual",
                "confidence": "manual",
            }
            changed = True
    return meta  # caller persists if changed


def save_seeds(seeds: Dict[str, str]) -> None:
    # Defensive: strip any sentinel keys (anything starting with "_")
    # that may have been left in by a hand-edit. cabal_detector.load_seed_cabals
    # coerces all values via str(v), which silently turns dict values into
    # Python reprs. Keeping the file pure {addr: "CabalName"} avoids that.
    seeds = {k: v for k, v in seeds.items() if not k.startswith("_") and isinstance(v, str)}
    CABAL_SEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CABAL_SEEDS_PATH, "w") as f:
        json.dump(seeds, f, indent=2)


def save_meta(meta: Dict[str, Dict[str, Any]]) -> None:
    with open(CABAL_SEEDS_META, "w") as f:
        json.dump(meta, f, indent=2)


def append_expired_audit(entries: List[Tuple[str, str, Dict[str, Any]]]) -> None:
    """Append expired entries to the audit log (one JSON object per line)."""
    if not entries:
        return
    CABAL_SEEDS_EXPIRED.parent.mkdir(parents=True, exist_ok=True)
    # Append as NDJSON for easy grep / ingest.
    with open(CABAL_SEEDS_EXPIRED, "a") as f:
        for addr, label, m in entries:
            row = {
                "ts": int(time.time()),
                "wallet_address": addr,
                "label": label,
                "meta": m,
            }
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# TTL + label logic
# ---------------------------------------------------------------------------


def _pick_label(u: UnifiedRow) -> str:
    """Pick a label for a brand-new (or freshly updated) wallet."""
    short = _short_addr(u.wallet_address)
    if u.confidence == HIGH_CONFIDENCE and len(u.sources) >= 2:
        # Cross-source: name hint is the alphabetically first source id
        hint = "+".join(sorted(set(u.sources)))
        return f"{hint}-{short}"
    src = u.sources[0] if u.sources else "unknown"
    return f"auto-{src}-{short}"


def _apply_ttl(
    seeds: Dict[str, str],
    meta: Dict[str, Dict[str, Any]],
    unified: Dict[str, UnifiedRow],
    ttl_days: int,
    *,
    min_pnl_sol: Optional[float] = None,
    min_confidence: str = "low",
) -> Tuple[List[str], List[Tuple[str, str, Dict[str, Any]]]]:
    """Compute which seeds to keep / expire / add. Returns (added, expired).

    Expiration rule:
      A seed entry is expired only if its `last_active_ts` is older than
      `--ttl-days` AND `not still_holding` AND it is NOT present in any
      current source's unified output. (If a wallet is currently being
      reported by a live source, its meta gets refreshed in step 2 and
      its expiration window resets naturally.)

    Addition rule:
      A wallet seen in any source but missing from seeds gets a new
      label + meta. (High-confidence if seen by 2+ sources.)
    """
    ttl_seconds = ttl_days * 86400
    now = int(time.time())
    expired: List[Tuple[str, str, Dict[str, Any]]] = []

    # 1. Expiration pass: only evict seeds that are stale AND not currently
    # being reported by any live source. This prevents the "expire then
    # immediately re-add in the same run" loop.
    live_addrs = set(unified.keys())
    for addr in list(seeds.keys()):
        if addr in live_addrs:
            # Live signal trumps stale meta — skip expiration; the add
            # pass below will refresh this entry's meta to "now".
            continue
        m = meta.get(addr) or {}
        last_active = _safe_int(m.get("last_active_ts"))
        still_holding = bool(m.get("still_holding", False))
        if last_active and (now - last_active) > ttl_seconds and not still_holding:
            label = seeds[addr]
            expired.append((addr, label, m))
            del seeds[addr]
            meta.pop(addr, None)

    # 2. Add pass: for each new wallet, decide label + meta.
    added: List[str] = []
    for u in unified.values():
        addr = u.wallet_address
        if addr in seeds:
            # Already known — refresh last_active_ts & still_holding so
            # a re-sighting pushes back the TTL.
            m = meta.setdefault(addr, {})
            m["last_active_ts"] = max(
                _safe_int(m.get("last_active_ts")),
                u.last_active_ts,
            )
            m["still_holding"] = bool(
                m.get("still_holding", False) or u.still_holding
            )
            m.setdefault("discovered_ts", u.discovered_ts)
            m.setdefault("source", u.sources[0] if u.sources else "unknown")
            m["sources_seen"] = sorted(set(u.sources))
            m["confidence"] = u.confidence
            m["buy_count"] = u.buy_count
            m["sell_count"] = u.sell_count
            m["volume_usd"] = u.volume_usd
            if u.realized_pnl_sol is not None:
                m["realized_pnl_sol"] = u.realized_pnl_sol
            continue

        # Confidence filter: skip if user requested only "high" (2+ sources)
        # and this wallet has only one source.
        if min_confidence == "high" and u.confidence != HIGH_CONFIDENCE:
            continue

        # PnL filter: skip if user requested a min_pnl_sol threshold and
        # the wallet's combined realized + unrealized doesn't clear it.
        # This is the key filter that keeps cabal_seeds clean of losing
        # wallets (which would otherwise trigger false-positive cabal
        # detections when other wallets share the same funder).
        if min_pnl_sol is not None:
            realized = u.realized_pnl_sol or 0.0
            unrealized = u.unrealized_pnl_estimate or 0.0
            # `total_pnl` uses (realized + unrealized). For wallets that
            # have fully exited (still_holding=False) unrealized is 0 so
            # this collapses to realized_pnl.
            total_pnl = realized + unrealized
            if total_pnl < min_pnl_sol:
                continue

        # New wallet → invent a label and record meta.
        seeds[addr] = _pick_label(u)
        meta[addr] = {
            "discovered_ts": u.discovered_ts,
            "last_active_ts": u.last_active_ts or int(time.time()),
            "still_holding": u.still_holding,
            "source": u.sources[0] if u.sources else "unknown",
            "sources_seen": sorted(set(u.sources)),
            "confidence": u.confidence,
            "buy_count": u.buy_count,
            "sell_count": u.sell_count,
            "volume_usd": u.volume_usd,
        }
        if u.realized_pnl_sol is not None:
            meta[addr]["realized_pnl_sol"] = u.realized_pnl_sol
        added.append(addr)

    return added, expired


# ---------------------------------------------------------------------------
# cabal_detector harvesting (--seed-from-cabal-detector)
# ---------------------------------------------------------------------------


def harvest_from_sniper_net(sniper_net_path: Path) -> List[Dict[str, Any]]:
    """Run cabal_detector over a sniper_net_batch (or single-mint report)
    JSON and return raw wallet rows for the sync pipeline.

    We extract wallet addresses that cabal_detector flagged in clusters —
    these are wallets that already share evidence of co-buy timing or
    shared-funder, so they're high-quality candidates for the seed DB.
    """
    if not sniper_net_path.exists():
        print(f"[sync] WARN: sniper_net file not found: {sniper_net_path}", file=sys.stderr)
        return []

    # Make cabal_detector importable from the Jiro root.
    if str(JIRO_ROOT) not in sys.path:
        sys.path.insert(0, str(JIRO_ROOT))

    try:
        from cabal_detector import analyze_report  # type: ignore
    except Exception as e:  # pragma: no cover
        print(f"[sync] WARN: cabal_detector import failed: {e}", file=sys.stderr)
        return []

    with open(sniper_net_path, "r") as f:
        data = json.load(f)

    harvested: List[Dict[str, Any]] = []
    reports: List[Dict[str, Any]]
    if isinstance(data, list):
        reports = data
    elif isinstance(data, dict) and "reports" in data:
        reports = data["reports"]
    elif isinstance(data, dict):
        reports = [data]
    else:
        reports = []

    for r in reports:
        # Each report is a sniper_net_batch entry OR a full single-mint report.
        # The full report has top_holders[]; the batch entry has summary only.
        holders = r.get("top_holders") or r.get("holders") or []
        if not holders:
            continue
        try:
            analyzed = analyze_report(r)
        except Exception as e:
            print(f"[sync] WARN: analyze_report failed: {e}", file=sys.stderr)
            continue
        cabal = analyzed.get("cabal") or {}
        clusters = cabal.get("clusters") or []
        flagged_wallets: Set[str] = set()
        for cluster in clusters:
            if cluster.get("type") in ("CABAL", "SUSPECT_CLUSTER"):
                for w in cluster.get("wallets") or []:
                    flagged_wallets.add(w.get("wallet") or "")
        for h in holders:
            w = h.get("wallet") or ""
            if not w:
                continue
            harvested.append({
                "wallet_address": w,
                "_source": "cabal_detector",
                "buy_count": _safe_int(h.get("buy_count")),
                "sell_count": _safe_int(h.get("sell_count")),
                "pnl_usd": h.get("pnl_sol"),
                "volume_usd": _safe_float(h.get("volume_usd")),
                "last_active_ts": _safe_int(h.get("first_buy_ts")),
                "still_holding": bool(h.get("win")),  # heuristic
                "confidence": "high" if w in flagged_wallets else "low",
                "label": "",
            })

    return harvested


# ---------------------------------------------------------------------------
# Sync report
# ---------------------------------------------------------------------------


def write_sync_report(
    added: List[str],
    expired: List[Tuple[str, str, Dict[str, Any]]],
    seeds: Dict[str, str],
    unified: Dict[str, UnifiedRow],
    source_counts: Dict[str, int],
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d", time.localtime())
    out_path = OUTPUT_DIR / f"sync_{ts}.json"

    # Top new = highest confidence + most active
    added_rows = [unified[a] for a in added if a in unified]
    top_new = sorted(
        added_rows,
        key=lambda u: (
            0 if u.confidence == HIGH_CONFIDENCE else 1,
            -u.volume_usd,
            -(u.last_active_ts or 0),
        ),
    )
    top_new_addrs = [u.wallet_address for u in top_new[:5]]

    report = {
        "ts": int(time.time()),
        "added": added,
        "added_count": len(added),
        "expired": [{"wallet_address": a, "label": l, "meta": m} for a, l, m in expired],
        "expired_count": len(expired),
        "kept_count": len(seeds),
        "kept": sorted(seeds.keys()),
        "by_source_counts": source_counts,
        "top_new": top_new_addrs,
        "top_new_rows": [r.to_dict() for r in top_new[:5]],
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return out_path


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(text: str) -> bool:
    """Best-effort Telegram notifier. Lazy import so we never block sync
    on the notifier module failing to load (or env being missing)."""
    if str(JIRO_ROOT) not in sys.path:
        sys.path.insert(0, str(JIRO_ROOT))
    try:
        from notifier import send  # type: ignore
        return send(text)
    except Exception as e:
        print(f"[sync] telegram skipped: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_table(
    added: List[str],
    expired: List[Tuple[str, str, Dict[str, Any]]],
    seeds: Dict[str, str],
    unified: Dict[str, UnifiedRow],
) -> None:
    def short(a: str) -> str:
        return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a

    print("\n=== SYNC DRY RUN ===")
    print(f"\nKept ({len(seeds)}):")
    for addr in sorted(seeds.keys())[:10]:
        print(f"  ✓ {short(addr)}  →  {seeds[addr]}")
    if len(seeds) > 10:
        print(f"  … and {len(seeds) - 10} more")

    if expired:
        print(f"\nExpired ({len(expired)}):")
        for addr, label, m in expired[:10]:
            last_active = _safe_int(m.get("last_active_ts"))
            age_days = (int(time.time()) - last_active) // 86400 if last_active else "?"
            print(f"  ✗ {short(addr)}  →  {label}  (last active ~{age_days}d ago)")
        if len(expired) > 10:
            print(f"  … and {len(expired) - 10} more")

    if added:
        print(f"\nAdded ({len(added)}):")
        added_rows = [unified[a] for a in added if a in unified]
        added_rows.sort(
            key=lambda u: (
                0 if u.confidence == HIGH_CONFIDENCE else 1,
                -u.volume_usd,
            )
        )
        for u in added_rows[:10]:
            print(
                f"  + {short(u.wallet_address)}  →  auto-{u.sources[0] if u.sources else '?'}-{_short_addr(u.wallet_address)}"
                f"   [{u.confidence}, {len(u.sources)} src, ${u.volume_usd:,.0f} vol]"
            )
        if len(added_rows) > 10:
            print(f"  … and {len(added_rows) - 10} more")

    print()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync top-trader data sources into cabal_seeds.json with TTL expiry.",
    )
    p.add_argument(
        "--source",
        choices=("onchain", "api_adapter", "gmgn", "all"),
        default="all",
        help="Which data source(s) to aggregate. Default: all.",
    )
    p.add_argument(
        "--ttl-days",
        type=int,
        default=14,
        help="Expire seed entries whose last_active_ts is older than N days AND still_holding=False. Default: 14.",
    )
    p.add_argument(
        "--min-pnl-sol",
        type=float,
        default=None,
        help=(
            "Only add wallets whose realized_pnl_sol + unrealized_pnl_estimate >= this. "
            "Default: None (add everyone). Examples: 0.5 for casual profit, 1.0 for "
            "strong performers, 5.0 for only whales. Set to a negative value to also "
            "add losing wallets (useful for cabal-membership signals)."
        ),
    )
    p.add_argument(
        "--min-confidence",
        choices=("low", "high"),
        default="low",
        help=(
            "Minimum source-confidence required to add a new wallet. "
            "'high' = must appear in 2+ sources (cross-confirmed). "
            "'low' = single-source ok. Default: low."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the kept/expired/added diff without writing anything.",
    )
    p.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip the Telegram notification even on add/expire.",
    )
    p.add_argument(
        "--seed-from-cabal-detector",
        metavar="PATH",
        help="Run cabal_detector over a sniper_net (batch or single-mint) JSON before aggregating, "
             "and include any wallets flagged CABAL/SUSPECT_CLUSTER in this sync.",
    )
    return p.parse_args(argv)


def _resolve_sources(arg: str) -> List[str]:
    if arg == "all":
        return [s for s in ALL_SOURCES]
    # Map CLI alias → internal id
    aliases = {
        "onchain": SOURCE_ONCHAIN,
        "api_adapter": SOURCE_API_ADAPTER,
        "gmgn": SOURCE_GMGN_SCRAPER,
    }
    return [aliases[arg]]


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    enabled = _resolve_sources(args.source)
    print(f"[sync] enabled sources: {enabled}", file=sys.stderr)

    # 1. Harvest from each source's output dir.
    raw = _load_all_sources(enabled)
    print(f"[sync] loaded {len(raw)} raw rows", file=sys.stderr)

    # 2. Optionally seed from cabal_detector (sniper_net output).
    if args.seed_from_cabal_detector:
        harvested = harvest_from_sniper_net(Path(args.seed_from_cabal_detector))
        print(f"[sync] harvested {len(harvested)} wallets from cabal_detector", file=sys.stderr)
        raw.extend(harvested)

    # 3. Schema-merge + dedup.
    unified = _merge_rows(raw)
    source_counts: Dict[str, int] = {}
    for u in unified.values():
        for s in u.sources:
            source_counts[s] = source_counts.get(s, 0) + 1
    print(
        f"[sync] unified {len(unified)} unique wallets. by_source={source_counts}",
        file=sys.stderr,
    )

    # 4. Load existing seeds + sidecar meta; migrate missing meta.
    seeds, meta = load_seeds_and_meta(write_ok=not args.dry_run)
    meta = migrate_meta(seeds, meta)
    print(f"[sync] loaded {len(seeds)} cabal seeds + {len(meta)} meta entries", file=sys.stderr)

    # 5. Apply TTL + additions.
    added, expired = _apply_ttl(
        seeds, meta, unified, ttl_days=args.ttl_days,
        min_pnl_sol=args.min_pnl_sol,
        min_confidence=args.min_confidence,
    )

    if args.dry_run:
        _print_table(added, expired, seeds, unified)
        print(
            f"[dry-run] NO writes performed. (would add {len(added)}, expire {len(expired)}, "
            f"keep {len(seeds)})",
            file=sys.stderr,
        )
        return 0

    # 6. Persist.
    if expired:
        append_expired_audit(expired)
        print(f"[sync] appended {len(expired)} expired entries to {CABAL_SEEDS_EXPIRED.name}", file=sys.stderr)
    save_seeds(seeds)
    save_meta(meta)
    print(
        f"[sync] wrote cabal_seeds.json ({len(seeds)} entries) and cabal_seeds.meta.json",
        file=sys.stderr,
    )

    # 7. Sync report.
    report_path = write_sync_report(added, expired, seeds, unified, source_counts)
    print(f"[sync] sync report → {report_path}", file=sys.stderr)

    # 8. Telegram — only when something changed.
    if (added or expired) and not args.no_telegram:
        top_new = sorted(
            [unified[a] for a in added if a in unified],
            key=lambda u: (0 if u.confidence == HIGH_CONFIDENCE else 1, -u.volume_usd),
        )
        top_addrs = [u.wallet_address for u in top_new[:3]]
        top_str = ", ".join(top_addrs) if top_addrs else "(none)"
        text = (
            f"🔄 Jiro smart-wallet sync: "
            f"+{len(added)} added, -{len(expired)} expired, {len(seeds)} kept. "
            f"Top new: {top_str}"
        )
        ok = send_telegram(text)
        print(f"[sync] telegram ok={ok}", file=sys.stderr)
    else:
        print("[sync] no changes — telegram skipped", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())