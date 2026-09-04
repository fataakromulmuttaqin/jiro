# smart_wallet_sync — Jiro Sniper Net module

Wires the three top-trader data sources into the user's
`cabal_seeds.json`, with **TTL-based expiration** so that
rotated/abandoned wallets get pruned automatically.

## Why TTL?

A wallet that has been "exposed" by appearing in a public top-traders
panel will be drained and abandoned by its operator within days. Keeping
it in the seed DB past its useful life wastes the +0.3 "known cabal"
boost on a ghost address — false positives. So every entry carries
`last_active_ts` and `still_holding`; if `last_active_ts` is older than
`--ttl-days` AND `still_holding` is False, the entry is expired.

## What it does

1. Walks the output directories of each top-trader source:
   - `api_adapter/output/` — `{CA}_traders.json` (DexScreener + GMGN + Birdeye + onchain_rpc merged)
   - `gmgn_scraper/output/` — `{CA}_traders.json` (gmgn.ai stealth or stub fallback)
   - `onchain/output/` — `{CA}_traders.json` (on-chain via Helius)

2. Schema-merges the per-row dicts into a unified row:
   ```
   {wallet_address, sources[], buy_count, sell_count, realized_pnl_sol,
    volume_usd, last_active_ts, still_holding, discovered_ts}
   ```
   Field-name variations between sources are normalised automatically
   (e.g. `buy_count` vs `buys`, `pnl_usd` vs `realized_pnl_sol`,
   `volume_30d` vs `buy_volume_sol`).

3. Deduplicates by `wallet_address`. A wallet seen in 2+ sources is
   tagged `high_confidence` and labelled `"{src1}+{src2}-{short_addr}"`.
   A wallet seen by only one source is `single_source` and labelled
   `"auto-{source}-{short_addr}"`.

4. Loads the existing `cabal_seeds.json` (flat
   `{address: "CabalName"}`) and a sidecar `cabal_seeds.meta.json`
   (TTL/hydration info).

5. **Migration**: any seed entry that has no entry in the sidecar meta
   file gets auto-bootstrapped with
   `_meta = {discovered_ts: today, last_active_ts: today, still_holding: True}`.
   The user manually curated it → we assume it's still good.

6. **TTL expiration**:
   - A seed entry is expired if `last_active_ts` is older than
     `--ttl-days` AND `not still_holding` AND it is NOT currently
     appearing in any live source's output. (Live signal trumps stale
     meta — if a source just reported this wallet, its meta gets
     refreshed to "now" instead of expiring.)
   - On `--dry-run`: prints a kept/expired/added table and exits.
   - On real run: appends expired entries to `cabal_seeds.expired.json`
     (NDJSON audit log) and removes them from `cabal_seeds.json`. Adds
     new entries with their generated labels.

7. Writes a sync report at
   `~/ruangkerja/jiro/top_traders/sync/output/sync_{YYYY-MM-DD}.json`:
   ```
   {added[], expired[], kept[], by_source_counts: {...}, top_new[]}
   ```

8. Sends a Telegram notification via `notifier.send()` ONLY when
   something was added or expired — not on no-op syncs. (Dry-run never
   sends, even if the diff is non-empty.)

## Schema decisions

- **cabal_seeds.json stays flat `{address: "CabalName"}`** — never
  nested with `_meta`. `cabal_detector.load_seed_cabals()` does
  `str(k): str(v)` and would silently coerce a nested `_meta` dict
  into a corrupted string label.
- **`cabal_seeds.meta.json`** is the sidecar: `{"<addr>": {_meta}}`.
  This is created automatically the first time sync runs.
- **`cabal_seeds.expired.json`** is the append-only audit log
  (NDJSON — one expired entry per line, plus `ts`, `wallet_address`,
  `label`, and a snapshot of meta at expiration time).

## CLI

```bash
python smart_wallet_sync.py                              # all sources, 14-day TTL, real run
python smart_wallet_sync.py --dry-run                    # preview only, no writes
python smart_wallet_sync.py --source api_adapter         # single source
python smart_wallet_sync.py --ttl-days 7 --dry-run
python smart_wallet_sync.py --seed-from-cabal-detector /path/to/sniper_net_batch.json
python smart_wallet_sync.py --no-telegram                # skip Telegram notification
```

## Cron snippet (every 30 min, only sync when output dirs have new files)

```cron
# smart_wallet_sync — pull fresh top-trader data into cabal_seeds.json
# every 30 minutes. Skips work when no source output file has been
# modified in the last 30 min (the script also handles this internally
# by writing idempotent output, but the find check avoids cron spam).
*/30 * * * * cd ~/ruangkerja/jiro && \
  find top_traders/api_adapter/output top_traders/gmgn_scraper/output top_traders/onchain/output \
    -name '*_traders.json' -mmin -30 -print -quit 2>/dev/null | \
  grep -q . && \
  python3 top_traders/sync/smart_wallet_sync.py --no-telegram \
    >> ~/ruangkerja/jiro/cache/smart_wallet_sync.log 2>&1 || true
```

The `find … -mmin -30 -print -quit` is the "only sync if output dirs
have new files" gate. If no `_traders.json` has been touched in the
last 30 min, `find` exits non-zero on the `-quit` and `grep -q .`
short-circuits the chain → cron no-ops. If at least one is fresh,
`smart_wallet_sync.py` runs and writes (idempotently) to
`cabal_seeds.json`, `cabal_seeds.meta.json`, `cabal_seeds.expired.json`,
and the daily sync report.

> **Tip**: Drop the `--no-telegram` flag if you want real Telegram
> alerts on add/expire events. Requires `TELEGRAM_BOT_TOKEN` and
> `TELEGRAM_CHAT_ID` in your environment (handled by the existing
> `notifier.py`).

## Output files (after first run)

| File | Purpose |
| --- | --- |
| `~/ruangkerja/jiro/cabal_seeds.json` | Flat `{addr: "CabalName"}` for `cabal_detector` |
| `~/ruangkerja/jiro/cabal_seeds.meta.json` | `{addr: {discovered_ts, last_active_ts, still_holding, source, ...}}` for TTL |
| `~/ruangkerja/jiro/cabal_seeds.expired.json` | NDJSON audit log of expired entries |
| `~/ruangkerja/jiro/top_traders/sync/output/sync_{YYYY-MM-DD}.json` | Daily sync report |

## Integration with cabal_detector

`cabal_detector.load_seed_cabals()` reads `cabal_seeds.json` as-is. The
`cabal_seeds.meta.json` sidecar is ignored by cabal_detector and is
consumed only by `smart_wallet_sync.py`. So you can freely re-run
`smart_wallet_sync.py` without breaking cabal detection.

## Tested against

- Test CA: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`
- First sync run: 45 wallets added (DexScreener 30 + GMGN 10 + onchain 5)
- Idempotent: second run produces 0 added / 0 expired / 49 kept
- Expiration: injected 2 stale seeds → both got moved to `cabal_seeds.expired.json`
- cabal_detector still parses cabal_seeds.json correctly after every run