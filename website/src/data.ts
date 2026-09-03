/**
 * Jiro Sniper Net — data loader.
 * Static JSON files served from /public/data/. No backend.
 */

import type { SniperNetReport, BatchSummary, ReportManifest } from './types';

const BASE = '/data';

/** Format a unix timestamp (seconds) to a human-readable date string. */
export function formatTs(ts: number | null | undefined): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

/** Shorten a Solana address for display: first 4 + last 4. */
export function shortAddr(address: string, head = 4, tail = 4): string {
  if (!address || address.length < head + tail + 1) return address || '—';
  return `${address.slice(0, head)}…${address.slice(-tail)}`;
}

/** Convert SOL to a fixed-decimal display string. */
export function fmtSol(sol: number | null | undefined, decimals = 4): string {
  if (sol === null || sol === undefined) return '—';
  return `${sol >= 0 ? '+' : ''}${sol.toFixed(decimals)}`;
}

/** Format roi_pct with sign and 2 decimals. */
export function fmtPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return '—';
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

/** Fetch the manifest of all reports. Returns empty if not found. */
export async function fetchManifest(): Promise<ReportManifest | null> {
  try {
    const r = await fetch(`${BASE}/manifest.json?t=${Date.now()}`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/** Fetch a single mint's report by its short hash (first 8 chars). */
export async function fetchReport(mintShort: string): Promise<SniperNetReport | null> {
  try {
    const r = await fetch(`${BASE}/sniper_net_${mintShort}.json?t=${Date.now()}`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/** Fetch the batch summary (latest analysis run). */
export async function fetchBatch(): Promise<BatchSummary | null> {
  try {
    const r = await fetch(`${BASE}/sniper_net_batch.json?t=${Date.now()}`);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/** Color class for a wallet's PnL. */
export function pnlColorClass(pnl: number | null | undefined): string {
  if (pnl === null || pnl === undefined) return 'text-steel';
  if (pnl > 0) return 'text-emerald';
  if (pnl < 0) return 'text-red';
  return 'text-steel';
}

/** Color class for a cabal cluster type. */
export function clusterColorClass(type: string): string {
  if (type === 'CABAL') return 'cabal';
  if (type === 'SUSPECT_CLUSTER') return 'suspect';
  return 'solo';
}

/** CSS class name for a behavior tag (lowercase, snake_case compatible). */
export function behClass(tag: string): string {
  return `beh-${tag.toLowerCase().replace(/_/g, '_')}`;
}

/** Fetch the cabal seeds (known funder addresses → cabal names). */
export async function fetchCabalSeeds(): Promise<Record<string, string>> {
  try {
    const r = await fetch(`${BASE}/cabal_seeds.json?t=${Date.now()}`);
    if (!r.ok) return {};
    const data = await r.json();
    if (data && typeof data === 'object') {
      // strip _comment / _examples keys
      const cleaned: Record<string, string> = {};
      for (const [k, v] of Object.entries(data)) {
        if (!k.startsWith('_') && typeof v === 'string') {
          cleaned[k] = v;
        }
      }
      return cleaned;
    }
    return {};
  } catch {
    return {};
  }
}

/** Solana explorer URL for a wallet. */
export function solscanWallet(address: string): string {
  return `https://solscan.io/account/${address}`;
}

/** Solana explorer URL for a token mint. */
export function solscanToken(mint: string): string {
  return `https://solscan.io/token/${mint}`;
}

/** Dexscreener URL for a token mint. */
export function dexscreenerUrl(mint: string): string {
  return `https://dexscreener.com/solana/${mint}`;
}