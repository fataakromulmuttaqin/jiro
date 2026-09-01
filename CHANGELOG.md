# Changelog

All notable changes to Jiro are documented here. Format follows Keep-a-Changelog.

## [Unreleased]

### Fixed — bugfix pass 2026-09-02 (cleanup + hardening bugs)
- **`open_position` silent-no-op of new entry signals (important)**: `gap_finder_bot`
  passes the candidate dict without a `mint` key and the mint as a separate arg.
  `compute_entry_score()` and `passes_entry()` gate the smart-money convergence
  bonus and the holder/rug screen on `mint in candidate`, so those two signals
  from the entry-side hardening pass **never fired on the real entry path**.
  `open_position()` now injects the resolved `token_mint` into the candidate
  before scoring, so both signals actually run. Covered by a regression test.
- **`smart_money.py` memory leak**: `_seen_signatures` was never trimmed, so it
  grew without bound over a 24/7 run. It is now pruned to a rolling window
  (older than the convergence window), tracked per-signature timestamp.
- **`holder_analyzer.py` memory leak**: `_screen_cache` never evicted entries
  (TTL only blocked stale *reads*), so it grew for every mint ever screened.
  Added a hard size cap with oldest-entry eviction.
- Removed obsolete `filetambahan/` staging folder (integration already applied).

### Added — entry-side hardening pass 2026-09-02
- **Holder distribution / rug screen** (`holder_analyzer.py`): pre-entry
  counterpart to `onchain_analyzer.py` (which is exit-only). Screens top
  holders, mint & freeze authority, dev-wallet hold (pump.fun-origin only),
  fresh-wallet %, and bundler-cluster % (wallets sharing the same SOL
  funding source). 20-minute in-memory cache. Hard-rejects tokens failing
  the configured thresholds; folds residual `risk_score` into
  `entry_score` (0–3 point penalty).
- **Smart-money convergence signal** (`smart_money.py`): watchlist-based
  tracker. You curate `watchlist.json` (see `watchlist.example.json`)
  with known-good wallets. Every scan cycle the bot fetches each watched
  wallet's recent transactions and detects token buys. When 2+ watched
  wallets buy the same mint within the configured window, the entry
  score gets a configurable bonus (default +1.5). Free-RPC-only — no
  Nansen/Cielo dependency. `watchlist.json` is gitignored.
- **New `config.json` sections**: `holder_filters` and `smart_money`,
  both `enabled: true` by default but each individual feature can be
  tuned or disabled. See README "Tuning everything".
- **Wiring**:
  - `trading.compute_entry_score()` adds smart-money convergence bonus
    (soft signal — raises score if hit).
  - `trading.passes_entry()` runs holder screen after narrative +
    on-chain hard-filters pass; rejects outright on hard violations,
    folds risk into score on soft warnings.
  - `gap_finder_bot.run_once()` calls `smart_money.poll_watchlist()`
    once per scan cycle (before candidate evaluation) so convergence
    checks are cheap in-memory lookups.
- **Test suite**: added 21 unit tests across `tests/test_holder_analyzer.py`
  (authority rejection, top10 concentration, risk-score clamping,
  cache hit) and `tests/test_smart_money.py` (watchlist loading,
  buy extraction from txs, convergence detection, deduplication).
  Full suite: 66 tests passing.

### Added — hardening pass 2026-09-01
- **RPC failover** (`rpc_client.py`): single-client wrapper with primary +
  fallback provider rotation, per-provider health tracking, automatic
  cooldown after failures. Tries `RPC_URL` (Helius) first, then any
  `RPC_FALLBACK_URLS` in order. Returns `None` only when every provider
  is unavailable, so callers can skip a cycle cleanly.
- **Safety gate** (`safety.py`): four-layer hard lock against accidental
  live trading. `config.json` cannot enable live mode. Requires explicit
  `ARM_LIVE_TRADE=YES-I-WANT-LIVE-MONEY-AT-RISK-2026` env token. Refuses
  specific pubkeys via `REFUSE_PUBKEYS`. `assert_safe_for_live()` runs
  at module import in `trading.py` and raises if env says live but guards
  aren't armed.
- **Test suite** (`tests/`): 38 unit tests for safety, RPC failover,
  on-chain exit signals, entry scoring, position lifecycle (TP/SL/trailing
  stop / partial TP / on-chain override / narrative decay / kill switch /
  max-open cap). All passing. Run with `pytest tests/`.
- **Bug caught by tests — safety logic**: original `arm_ok_to_trade()`
  had inverted `DRY_RUN` check (would refuse live trading when DRY_RUN
  was false, which is the opposite of intent). Fixed.
- **Bug caught by tests — trailing stop priority**: trailing stop was
  firing before stop-loss on the same condition, contradicting README's
  "SL is a hard floor, always active". Fixed: trailing stop now only
  fires when price is still at or above entry; otherwise SL wins.
- **`.env.example`** with primary/backup RPC, safety arm token, refuse
  pubkeys, and quick-start instructions.

## [0.1.0] — 2026-08-31

### Initial scaffold
- Memecoin gap-finder bot. Grok scans X for off-crypto viral narratives,
  Dexscreener checks for existing weak pairs, Jupiter swap execution,
  on-chain exit signals (whale dump, liquidity pull, sell pressure, fast
  dump), trailing stop, partial TP ladder, daily-loss kill switch.
- See README.md "Testing notes" for the two bugs already fixed during
  initial development (partial TP tranche size & trailing peak persistence).