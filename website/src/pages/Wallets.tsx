/**
 * Wallets page — aggregated view of all wallets across all mints.
 * Cross-mint leaderboard: who wins the most often, biggest PnL.
 */

import { useEffect, useState } from 'react';
import { fetchManifest, fetchReport, fmtSol, fmtPct, pnlColorClass, formatTs } from '../data';
import type { SniperNetReport, ReportManifest } from '../types';
import { WalletChip } from '../components/WalletChip';

interface AggregatedWallet {
  wallet: string;
  label: string;
  observations: number;
  wins: number;
  losses: number;
  total_pnl_sol: number;
  best_roi_pct: number | null;
  latest_mint: string;
  latest_ts: number;
  behavior_tags: string[];
}

function aggregate(reports: SniperNetReport[]): AggregatedWallet[] {
  const byWallet = new Map<string, AggregatedWallet>();
  for (const r of reports) {
    for (const p of r.top_holders) {
      if (!p.wallet) continue;
      const existing = byWallet.get(p.wallet);
      const winCount = p.win === true ? 1 : 0;
      const lossCount = p.win === false ? 1 : 0;
      if (!existing) {
        byWallet.set(p.wallet, {
          wallet: p.wallet,
          label: p.label,
          observations: 1,
          wins: winCount,
          losses: lossCount,
          total_pnl_sol: p.realized_pnl_sol ?? 0,
          best_roi_pct: p.roi_pct,
          latest_mint: r.mint,
          latest_ts: r.ts,
          behavior_tags: p.behavior_tags,
        });
      } else {
        existing.observations += 1;
        existing.wins += winCount;
        existing.losses += lossCount;
        existing.total_pnl_sol += p.realized_pnl_sol ?? 0;
        if (p.roi_pct !== null && (existing.best_roi_pct === null || p.roi_pct > existing.best_roi_pct)) {
          existing.best_roi_pct = p.roi_pct;
        }
        if (r.ts > existing.latest_ts) {
          existing.latest_ts = r.ts;
          existing.latest_mint = r.mint;
          existing.behavior_tags = p.behavior_tags;
          existing.label = p.label;
        }
      }
    }
  }
  return Array.from(byWallet.values());
}

export function Wallets() {
  const [manifest, setManifest] = useState<ReportManifest | null>(null);
  const [reports, setReports] = useState<SniperNetReport[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchManifest().then(async (m) => {
      if (!m) { setLoading(false); return; }
      setManifest(m);
      const loaded = await Promise.all(m.mints.map((mi) => fetchReport(mi.mint.slice(0, 8))));
      setReports(loaded.filter((r): r is SniperNetReport => r !== null));
      setLoading(false);
    });
  }, []);

  if (loading) return <div className="p-8 font-mono text-sm text-steel">Loading…</div>;
  if (!manifest || reports.length === 0) {
    return <div className="p-8 font-mono text-sm text-steel">No data available.</div>;
  }

  const wallets = aggregate(reports).sort((a, b) => b.total_pnl_sol - a.total_pnl_sol);
  const totalPnl = wallets.reduce((acc, w) => acc + w.total_pnl_sol, 0);

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="font-display text-2xl text-cream tracking-wider">WALLETS</h1>
        <p className="font-mono text-xs text-steel mt-2">
          {wallets.length} unique wallets · aggregated across {reports.length} mints
        </p>
      </div>

      <div className="stat-card p-4 mb-6 font-mono text-xs text-steel">
        <span className="text-cream">Total realized PnL across all wallets:</span>{' '}
        <span className={pnlColorClass(totalPnl)}>{fmtSol(totalPnl)} SOL</span>
      </div>

      <div className="stat-card p-6">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="text-steel uppercase tracking-wider border-b border-cyan-dim">
              <th className="text-left py-2">#</th>
              <th className="text-left py-2">Wallet</th>
              <th className="text-left py-2">Label</th>
              <th className="text-right py-2">Obs</th>
              <th className="text-right py-2">W/L</th>
              <th className="text-right py-2">Total PnL</th>
              <th className="text-right py-2">Best ROI</th>
              <th className="text-left py-2">Behavior</th>
              <th className="text-right py-2">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {wallets.map((w, i) => (
              <tr key={w.wallet} className="border-b border-cyan-dim/30 hover:bg-cyan-dim/20">
                <td className="py-2 text-steel">{i + 1}</td>
                <td className="py-2"><WalletChip address={w.wallet} /></td>
                <td className="py-2 text-cream">{w.label}</td>
                <td className="py-2 text-right text-steel">{w.observations}</td>
                <td className="py-2 text-right">
                  <span className="text-emerald">{w.wins}</span>
                  <span className="text-steel mx-1">/</span>
                  <span className="text-red">{w.losses}</span>
                </td>
                <td className={`py-2 text-right ${pnlColorClass(w.total_pnl_sol)}`}>
                  {fmtSol(w.total_pnl_sol)}
                </td>
                <td className={`py-2 text-right ${pnlColorClass((w.best_roi_pct ?? 0) > 0 ? 1 : -1)}`}>
                  {fmtPct(w.best_roi_pct)}
                </td>
                <td className="py-2">
                  {w.behavior_tags.length > 0 && (
                    <span className="tag-pill beh-solo">{w.behavior_tags[0]}</span>
                  )}
                </td>
                <td className="py-2 text-right text-steel text-[10px]">
                  {formatTs(w.latest_ts)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}