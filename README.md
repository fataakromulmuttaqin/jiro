# Memecoin Gap-Finder + Smart Auto-Trade Bot

Strategy: watch what's viral among **normies on X** (not CT), find the gap
before crypto twitter tokenizes it, score entries with narrative + on-chain
signals, and manage exits with trailing stop / partial take-profit / on-chain
dump detection / narrative-decay detection.

## Files
- `config.py` — loads/creates `config.json`, the single place to tune every
  trading number (entry size, TP/SL %, trailing stop, partial TP ladder,
  on-chain thresholds, holder filters, smart-money thresholds, timing).
- `narrative.py` — Grok calls: scan X for new viral off-crypto narratives,
  and re-check narrative health (accelerating/peaking/declining/dead) for
  positions already open.
- `onchain_analyzer.py` — polls Solana RPC + Dexscreener to detect whale
  dumps, liquidity pulls, sell-pressure flips, and fast price-velocity dumps.
- `holder_analyzer.py` — ENTRY-time holder distribution / rug screen.
  Checks top10 %, dev-wallet hold (pump.fun-origin only), fresh-wallet %,
  bundler-cluster % (shared SOL funder), and mint/freeze authority. Hard
  rejects tokens failing the configured thresholds; folds residual risk
  into `entry_score`. 20-minute in-memory cache.
- `smart_money.py` — ENTRY-time watchlist tracker. You curate
  `watchlist.json` with known-good wallets; every scan cycle the bot
  fetches each one's recent transactions and detects token buys. When
  2+ watched wallets buy the same mint within the configured window,
  the entry score gets a bonus. Free RPC only — no Nansen/Cielo
  dependency. Watchlist is gitignored.
- `trading.py` — Jupiter swap execution, entry scoring, position tracking,
  trailing stop, partial take-profit, on-chain + narrative exit signals,
  daily-loss kill switch.
- `gap_finder_bot.py` — orchestrates the above: scan → gap check → entry
  score → (optional) open → periodic narrative recheck → (optional) monitor.

## Install
```bash
pip install requests solders base58 --break-system-packages
# optional: ML/ANN pump-probability filter (ml_filter.py) needs numpy + sklearn
#   pip install numpy scikit-learn joblib --break-system-packages
```

## Setup (research/alerts only — safe default, no wallet needed)
```bash
export XAI_API_KEY=your_xai_key          # console.x.ai
export TELEGRAM_BOT_TOKEN=xxx            # optional
export TELEGRAM_CHAT_ID=xxx              # optional

python3 gap_finder_bot.py --loop
```

## Setup (auto-trading — real money, real risk)
```bash
export SOLANA_PRIVATE_KEY=your_base58_secret_key   # use a BURNER wallet
export RPC_URL=https://your-rpc-provider-url        # Helius/QuickNode/Triton
export AUTO_TRADE_ENABLED=true
export DRY_RUN=true          # keep true until you've watched it run

python3 gap_finder_bot.py --loop --with-monitor
```
Go fully live only after watching dry-run logs and setting `DRY_RUN=false`
deliberately. Keep `config.json`'s `position_size_usd` small at first and use
a dedicated burner wallet funded only with what you're OK losing entirely.

## Tuning everything (`config.json`)
Edit this file directly — no code changes, and the running bot picks up
changes on its next cycle (config is reloaded each loop iteration):

```jsonc
{
  "trading": {
    "position_size_usd": 50,           // $ per trade
    "max_open_positions": 3,
    "take_profit_pct": 60,             // used only if trailing_stop_enabled=false
    "stop_loss_pct": 25,               // hard floor, always active
    "trailing_stop_enabled": true,     // let winners run instead of capping at fixed TP
    "trailing_stop_activate_pct": 30,  // trailing kicks in once up 30%
    "trailing_stop_distance_pct": 15,  // trails 15% behind the peak price
    "partial_take_profit": [           // scale out in tranches
      {"at_pct": 40, "sell_pct": 50},  // sell 50% of ORIGINAL size at +40%
      {"at_pct": 100, "sell_pct": 50}  // sell remaining 50% at +100%
    ],
    "max_daily_loss_usd": 50           // kill switch
  },
  "entry_filters": {
    "min_entry_score": 6.0,            // 0-10 combined narrative+onchain score
    "min_liquidity_usd": 2000,
    "max_liquidity_usd": 80000,        // above this, the gap is probably gone
    "min_buy_sell_ratio_h1": 1.1,
    "max_price_impact_pct": 8
  },
  "onchain_exit_signals": {
    "whale_dump_threshold_pct": 15,    // top-10 holders' balance drop -> exit
    "liquidity_pull_threshold_pct": 20,// sudden LP drop -> exit (rug signal)
    "sell_pressure_ratio_trigger": 0.35,
    "fast_dump_price_drop_pct": 12,    // price velocity dump -> exit
    "fast_dump_window_seconds": 180
  },
  "system": {
    "poll_interval_minutes": 15,       // how often Grok scans X
    "monitor_interval_seconds": 20,    // how often positions are checked
    "narrative_recheck_every_n_scans": 1
  }
}
```

## How the "smart" entry/exit decisions actually work
**Entry** — `trading.compute_entry_score()` combines: narrative strength
(post volume, cross-community spread, still-organic, CT notice level) with
on-chain momentum (liquidity in a sane range, buy/sell ratio, estimated
price impact of your own position size). Hard filters (liquidity floor/ceiling,
minimum buy/sell ratio, max price impact) reject outright regardless of
score; everything else needs `min_entry_score` to pass.

**Exit**, checked every `monitor_interval_seconds`, in priority order:
1. **On-chain dump signals** (`onchain_analyzer.py`) — whale balance drop,
   liquidity pull, sell-pressure flip, or fast price-velocity dump. Any one
   firing closes the position immediately, overriding TP/SL.
2. **Narrative decay** — if a slower-cadence Grok recheck says the meme is
   `declining`/`dead` while the position is in profit, close and lock gains
   rather than wait for price to fully catch down.
3. **Trailing stop** (if enabled) — ratchets up with the price peak, exits
   on a pullback past the trail distance. This intentionally overrides the
   fixed take-profit so winners aren't capped early.
4. **Partial take-profit ladder** — sells configured tranches of the
   *original* position size at each profit level; remainder keeps running
   under the trailing stop.
5. **Stop-loss** — a hard floor, always active regardless of the above.
6. **Fixed take-profit** — only used as the exit mechanism if
   `trailing_stop_enabled` is `false`.

## Safety mechanisms
- **Dry-run by default** — no real transaction until you explicitly set both
  `AUTO_TRADE_ENABLED=true` and `DRY_RUN=false`.
- **Sellability pre-check** — SOL→token→SOL round-trip quote before buying,
  to catch pools that can't route back (a common honeypot signature). Not
  foolproof against contract-level sell-blocking.
- **Position cap and fixed USD sizing** — no martingale, no scaling into losers.
- **Daily loss kill switch** — stops opening new positions once realized
  losses in the last 24h hit `max_daily_loss_usd`. Reset by clearing `ledger.json`.
- **Every position gets both TP and SL** computed at entry, checked every cycle.

## Testing notes (what was actually verified before shipping this)
All modules were compile-checked, and the full trading logic was exercised
with a mocked price/quote feed (this sandbox can't reach Jupiter/Dexscreener/
Solana RPC directly) covering: partial take-profit accounting across multiple
tranches, stop-loss as a hard floor, trailing stop overriding fixed TP and
ratcheting up on new peaks, on-chain emergency exit overriding a profitable
hold, narrative-decay exit while in profit, entry rejection on bad liquidity/
buy-sell ratio, the max-open-positions cap, and the daily-loss kill switch.
Two real bugs were caught and fixed this way: (1) partial take-profit was
computing tranche size against *remaining* tokens instead of the *original*
position, leaving a dangling 25% unsold after two "50%" sells; (2) the
trailing-stop's peak/trail price was never persisted to disk on a plain
"hold" cycle, silently resetting it every loop. Both are fixed and covered
by the tests above. This does **not** mean the bot is bug-free in live
market conditions — untested against a real RPC/Jupiter/Dexscreener under
load, and no amount of testing substitutes for a real audit. Watch dry-run
logs closely before trusting it with money.

## What this still can't protect you from
- A rug that completes faster than your exit transaction confirms.
- Extreme slippage on illiquid pools even with slippage limits set.
- RPC congestion delaying your sell past your intended exit.
- Contract-level honeypots that block sells in ways a quote won't reveal.
- The underlying thesis being wrong — a viral moment never getting tokenized,
  or getting tokenized and dying instantly.
- Grok's read of "what's viral" being wrong or stale — it's a signal, not
  a certainty.

This is a scaffold for you to extend, test, and harden — not a finished,
audited trading product. Nothing here is financial advice.

