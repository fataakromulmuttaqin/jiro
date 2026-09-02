#!/usr/bin/env python3
"""
config.py — single source of truth for all TUNABLE trading parameters.

Secrets (API keys, RPC URL, wallet key) stay as environment variables
(see trading.py / gap_finder_bot.py docstrings) — they don't belong in a
JSON file you might accidentally commit or share.

Everything a trader would want to tune lives in config.json, created here
with defaults on first run. Edit that file directly; no code changes needed.
"""

import os
import json
import copy
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "trading": {
        # how much USD to spend per position (converted to SOL at entry time
        # using the live SOL price)
        "position_size_usd": 50,
        "max_open_positions": 3,
        "max_slippage_bps": 500,

        "take_profit_pct": 60,
        "stop_loss_pct": 25,

        # trailing stop: once profit passes `activate_pct`, the stop-loss
        # is pulled up to (current_price - trail_distance_pct), locking in
        # gains as price rises. Disable to use only the fixed TP/SL above.
        "trailing_stop_enabled": True,
        "trailing_stop_activate_pct": 30,
        "trailing_stop_distance_pct": 15,

        # partial take-profit: sell a % of the position at each profit
        # level instead of dumping everything at one TP price. Remaining
        # runs on the trailing stop (if enabled) or the fixed TP.
        # Example below: sell 50% at +40%, sell remaining at +100%.
        "partial_take_profit": [
            {"at_pct": 40, "sell_pct": 50},
            {"at_pct": 100, "sell_pct": 50}
        ],

        "max_daily_loss_usd": 50
    },

    "entry_filters": {
        # minimum combined narrative+onchain score (0-10) required to enter
        "min_entry_score": 6.0,
        "min_liquidity_usd": 2000,
        "max_liquidity_usd": 80000,     # above this, the gap is probably gone
        "min_buy_sell_ratio_h1": 1.1,    # buys must outnumber sells
        "max_price_impact_pct": 8        # est. impact of your own buy size
    },

    "launch_finder": {
        # how we find the token for a viral narrative: scan FRESH pump.fun
        # launches (bonding-curve only, i.e. not yet on a listing DEX) and
        # fuzzy-match the narrative term against symbol/name. Tokens already
        # on a listing exchange have usually pumped out of the gap.
        "max_age_hours": 6,          # only launches younger than this
        "max_market_cap_usd": 100000 # above this MC = probably already pumped
        ,"min_name_similarity": 0.72 # 0-1 fuzzy-match threshold vs narrative term
    },

    "ml_filter": {
        # ML/ANN pump-probability filter (from '151 Trading Strategies' §18.2).
        # Adds a mild ±score bonus to entry based on the ANN's predicted pump
        # probability. Strictly optional/additive: if the model or numpy is
        # missing, entries proceed exactly as without ML (never blocks).
        "enabled": True,
        "score_weight": 1.0
    },

    "onchain_exit_signals": {
        "enabled": True,
        "whale_dump_threshold_pct": 15,     # top holders' combined balance drop
        "whale_check_top_n_holders": 10,
        "whale_check_window_minutes": 10,
        "liquidity_pull_threshold_pct": 20,  # sudden LP drop = likely rug
        "liquidity_check_window_minutes": 5,
        "sell_pressure_ratio_trigger": 0.35,  # buys/(buys+sells) below this = heavy selling
        "fast_dump_price_drop_pct": 12,
        "fast_dump_window_seconds": 180
    },

    "system": {
        "poll_interval_minutes": 15,      # how often to scan X via Grok
        "monitor_interval_seconds": 20,   # how often to check open positions
        "narrative_recheck_every_n_scans": 1  # re-ask Grok about open positions every N scans
    }
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_PATH) as f:
            user_cfg = json.load(f)
    except Exception as e:
        print(f"[config] could not parse config.json ({e}), using defaults")
        return copy.deepcopy(DEFAULT_CONFIG)

    # merge so new keys added in future versions always exist, without
    # clobbering values the user already customized
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    if merged != user_cfg:
        save_config(merged)  # persist newly-added defaults back to disk
    return merged


def save_config(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


if __name__ == "__main__":
    cfg = load_config()
    print(f"Config at {CONFIG_PATH}:")
    print(json.dumps(cfg, indent=2))
