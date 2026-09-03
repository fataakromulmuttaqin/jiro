/**
 * Jiro Sniper Net — type definitions matching the Python pipeline JSON output.
 * Source of truth: wallet_profiler.py + cabal_detector.py + behavior_miner.py
 */

export interface WalletProfile {
  wallet: string;
  label: string;
  mint: string;
  tx_count: number;
  mint_tx_count: number;
  first_buy_ts: number | null;
  last_action_ts: number | null;
  buys_sol: number;
  sells_sol: number;
  realized_pnl_sol: number;
  roi_pct: number | null;
  still_holds_pct: number;
  current_balance_ui: number;
  win: boolean | null;
  behavior_tags: string[];
  behavior_reason?: string;
  holder_pct?: number;
  error?: string;
}

export interface FunderEdge {
  from: string;
  to: string;
  amount_sol: number;
  ts: number | null;
  sig: string | null;
}

export interface FunderInfo {
  first_seen_as_funder?: number | null;
  sample_amount_sol?: number | null;
}

export interface CabalCluster {
  cluster_id: number;
  type: 'CABAL' | 'SUSPECT_CLUSTER' | 'SOLO';
  cabal_score: number;
  shared_funder: string | null;
  shared_funder_name: string | null;
  wallets: Array<{
    wallet: string;
    label: string;
    pnl_sol: number | null;
    win: boolean | null;
    first_buy_ts: number | null;
  }>;
  reason: string;
}

export interface BehaviorTag {
  wallet: string;
  label: string;
  tag: string;
  tag_reason: string;
  metrics: {
    held_seconds: number | null;
    still_holds_pct: number | null;
    roi_pct: number | null;
    first_buy_ts: number | null;
    last_action_ts: number | null;
  };
}

export interface WatchlistDiff {
  ts: number;
  added: Array<{
    address: string;
    label: string;
    added_ts: number;
    source: string;
    added_via_mint?: string;
  }>;
  pruned: Array<{
    address: string;
    label: string;
  }>;
  final_size: number;
  mint?: string;
}

export interface SniperNetReport {
  mint: string;
  ts: number;
  top_holders: WalletProfile[];
  funders: Record<string, string[]>;
  funder_details?: Record<string, FunderInfo>;
  cabal?: {
    ts: number;
    clusters: CabalCluster[];
    summary: {
      n_wallets: number;
      n_clusters: number;
      n_cabal: number;
      n_suspect: number;
      n_solo: number;
    };
  };
  behavior?: BehaviorTag[];
  watchlist_diff?: WatchlistDiff;
  summary?: {
    n_holders: number;
    n_winners: number;
    n_losers: number;
    total_pnl_sol: number;
    shared_funder_count: number;
  };
}

export interface BatchSummary {
  ts: number;
  n_mints: number;
  reports: Array<{
    mint: string;
    summary?: {
      n_wallets: number;
      n_clusters: number;
      n_cabal: number;
      n_suspect: number;
      n_solo: number;
    };
    n_winners: number;
    n_losers: number;
  }>;
}

/**
 * Manifest listing all known mints and their report files.
 * The website reads this at build/runtime to know what's available.
 */
export interface ReportManifest {
  generated_at: number;
  mints: Array<{
    mint: string;
    file: string;       // relative to /public/data/
    analyzed_at: number;
    n_holders: number;
    n_cabal: number;
    n_winners: number;
  }>;
}