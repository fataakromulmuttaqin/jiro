/**
 * Behavior page — archetype distribution + per-wallet tags.
 */

import { useEffect, useState } from 'react';
import { fetchManifest, fetchReport, fmtSol, pnlColorClass, formatTs } from '../data';
import type { SniperNetReport, BehaviorTag, ReportManifest } from '../types';
import { WalletChip } from '../components/WalletChip';

interface TaggedWallet extends BehaviorTag {
  pnl: number | null;
  mint: string;
}

function aggregate(reports: SniperNetReport[]): TaggedWallet[] {
  const out: TaggedWallet[] = [];
  for (const r of reports) {
    for (const t of r.behavior ?? []) {
      const p = r.top_holders.find((h) => h.wallet === t.wallet);
      out.push({
        ...t,
        pnl: p?.realized_pnl_sol ?? null,
        mint: r.mint,
      });
    }
  }
  return out;
}

const TAG_COLORS: Record<string, string> = {
  BUNDLER: 'beh-bundler',
  SNIPER: 'beh-sniper',
  EARLY_EXIT: 'beh-early_exit',
  DIAMOND_HAND: 'beh-diamond',
  WHALE: 'beh-whale',
  SCALPER: 'beh-scalper',
  SWING: 'beh-swing',
  EXIT_LIQUIDITY: 'beh-exit_liq',
  WINNER: 'beh-winner',
  LOSER: 'beh-loser',
  NEUTRAL: 'beh-neutral',
};

export function Behavior() {
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

  const wallets = aggregate(reports);
  const tagCounts = wallets.reduce<Record<string, number>>((acc, w) => {
    acc[w.tag] = (acc[w.tag] ?? 0) + 1;
    return acc;
  }, {});
  const sortedTags = Object.entries(tagCounts).sort((a, b) => b[1] - a[1]);

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="font-display text-2xl text-cream tracking-wider">BEHAVIOR</h1>
        <p className="font-mono text-xs text-steel mt-2">
          Archetype distribution + per-wallet behavior tags
        </p>
      </div>

      {/* Tag distribution */}
      <div className="stat-card p-6 mb-6">
        <h2 className="font-display text-xl text-cream mb-4">ARCHETYPE DISTRIBUTION</h2>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {sortedTags.map(([tag, count]) => (
            <div key={tag} className="bg-bg-primary rounded p-3 border border-cyan-dim/40">
              <div className="flex items-center gap-2 mb-1">
                <span className={`tag-pill ${TAG_COLORS[tag] ?? 'beh-neutral'}`}>{tag}</span>
              </div>
              <div className="font-display text-2xl text-cream">{count}</div>
              <div className="font-mono text-[10px] text-steel">
                {((count / wallets.length) * 100).toFixed(0)}% of tags
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Per-wallet table */}
      <div className="stat-card p-6">
        <h2 className="font-display text-xl text-cream mb-4">PER-WALLET TAGS</h2>
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="text-steel uppercase tracking-wider border-b border-cyan-dim">
              <th className="text-left py-2">Wallet</th>
              <th className="text-left py-2">Tag</th>
              <th className="text-left py-2">Reason</th>
              <th className="text-right py-2">PnL</th>
              <th className="text-right py-2">Holds%</th>
              <th className="text-right py-2">Analyzed</th>
            </tr>
          </thead>
          <tbody>
            {wallets.map((w) => (
              <tr key={`${w.wallet}-${w.mint}`} className="border-b border-cyan-dim/30 hover:bg-cyan-dim/20">
                <td className="py-2"><WalletChip address={w.wallet} label={w.label} /></td>
                <td className="py-2">
                  <span className={`tag-pill ${TAG_COLORS[w.tag] ?? 'beh-neutral'}`}>{w.tag}</span>
                </td>
                <td className="py-2 text-steel italic">{w.tag_reason}</td>
                <td className={`py-2 text-right ${pnlColorClass(w.pnl)}`}>
                  {fmtSol(w.pnl)}
                </td>
                <td className="py-2 text-right text-steel">
                  {w.metrics.still_holds_pct != null
                    ? `${w.metrics.still_holds_pct.toFixed(0)}%`
                    : '—'}
                </td>
                <td className="py-2 text-right text-steel text-[10px]">
                  {formatTs(w.metrics.last_action_ts)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}