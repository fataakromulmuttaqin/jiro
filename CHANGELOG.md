# Changelog

All notable changes to Jiro are documented here. Format follows Keep-a-Changelog.

## [Unreleased]

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