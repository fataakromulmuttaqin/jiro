/**
 * Single-mint detail page — full breakdown of one analysis run.
 */

import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { fetchReport, fmtSol, fmtPct, pnlColorClass, formatTs, behClass, dexscreenerUrl, solscanToken } from '../data';
import type { SniperNetReport } from '../types';
import { StatCard } from '../components/StatCard';
import { WalletChip } from '../components/WalletChip';

export function MintDetail() {
  const { mintShort } = useParams<{ mintShort: string }>();
  const [report, setReport] = useState<SniperNetReport | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!mintShort) return;
    fetchReport(mintShort).then((r) => {
      setReport(r);
      setLoading(false);
    });
  }, [mintShort]);

  if (loading) {
    return <div className="p-8 font-mono text-sm text-steel">Loading {mintShort}…</div>;
  }
  if (!report) {
    return (
      <div className="p-8">
        <div className="stat-card p-8 text-center">
          <div className="font-mono text-red text-sm">
            Report not found for mint <code>{mintShort}</code>
          </div>
          <Link to="/" className="text-cyan text-xs font-mono mt-3 inline-block">
            ← back to overview
          </Link>
        </div>
      </div>
    );
  }

  const cabal = report.cabal;
  const cabalSummary = cabal?.summary ?? { n_cabal: 0, n_suspect: 0, n_solo: 0, n_wallets: 0, n_clusters: 0 };
  const winners = report.top_holders.filter((p) => p.win === true);
  const totalPnl = report.top_holders.reduce((acc, p) => acc + (p.realized_pnl_sol ?? 0), 0);

  return (
    <div className="p-8">
      {/* Header */}
      <div className="mb-6">
        <Link to="/" className="text-cyan text-xs font-mono hover:text-emerald">
          ← overview
        </Link>
        <h1 className="font-display text-2xl text-cream mt-3 tracking-wider">
          MINT DETAIL
        </h1>
        <div className="font-mono text-sm text-steel mt-2">
          <a href={solscanToken(report.mint)} target="_blank" rel="noopener noreferrer"
             className="text-cyan hover:text-emerald">{report.mint}</a>
          {' · '}
          <a href={dexscreenerUrl(report.mint)} target="_blank" rel="noopener noreferrer"
             className="text-cyan hover:text-emerald">dexscreener ↗</a>
        </div>
        <div className="font-mono text-xs text-steel mt-1">
          analyzed {formatTs(report.ts)}
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
        <StatCard label="Holders" value={cabalSummary.n_wallets} accent="cyan" />
        <StatCard label="Cabals" value={cabalSummary.n_cabal} accent="magenta" />
        <StatCard label="Suspect" value={cabalSummary.n_suspect} accent="amber" />
        <StatCard label="Winners" value={winners.length} accent="emerald" />
        <StatCard label="Total PnL" value={`${fmtSol(totalPnl)} SOL`}
                   accent={totalPnl > 0 ? 'emerald' : totalPnl < 0 ? 'red' : 'steel'} />
      </div>

      {/* Cabal clusters */}
      {cabal && cabal.clusters.length > 0 && (
        <div className="stat-card p-6 mb-6">
          <h2 className="font-display text-xl text-cream mb-4">CABAL CLUSTERS</h2>
          <div className="space-y-3">
            {cabal.clusters.map((c) => (
              <div key={c.cluster_id} className="border border-cyan-dim rounded p-3">
                <div className="flex items-center gap-3 mb-2">
                  <span className={`tag-pill ${c.type === 'CABAL' ? 'cabal' : c.type === 'SUSPECT_CLUSTER' ? 'suspect' : 'solo'}`}>
                    {c.type}
                  </span>
                  <span className="font-mono text-xs text-steel">
                    score {c.cabal_score.toFixed(2)}
                  </span>
                  {c.shared_funder && (
                    <span className="font-mono text-xs text-steel">
                      funder: <WalletChip address={c.shared_funder} showLink={false} />
                      {c.shared_funder_name && ` (${c.shared_funder_name})`}
                    </span>
                  )}
                </div>
                <div className="font-mono text-xs text-steal mb-2 text-steel italic">
                  {c.reason}
                </div>
                <div className="flex flex-wrap gap-2">
                  {c.wallets.map((w) => (
                    <div key={w.wallet} className="inline-flex items-center gap-2 px-2 py-1 bg-bg-primary rounded">
                      <WalletChip address={w.wallet} label={w.label} />
                      <span className={`font-mono text-xs ${pnlColorClass(w.pnl_sol)}`}>
                        {fmtSol(w.pnl_sol)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Wallet profiles table */}
      <div className="stat-card p-6">
        <h2 className="font-display text-xl text-cream mb-4">WALLET PROFILES</h2>
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead>
              <tr className="text-steel uppercase tracking-wider border-b border-cyan-dim">
                <th className="text-left py-2">Wallet</th>
                <th className="text-left py-2">Label</th>
                <th className="text-right py-2">Holder%</th>
                <th className="text-right py-2">Buys (SOL)</th>
                <th className="text-right py-2">Sells (SOL)</th>
                <th className="text-right py-2">PnL</th>
                <th className="text-right py-2">ROI</th>
                <th className="text-right py-2">Holds%</th>
                <th className="text-left py-2">Behavior</th>
              </tr>
            </thead>
            <tbody>
              {report.top_holders.map((p) => (
                <tr key={p.wallet} className="border-b border-cyan-dim/30 hover:bg-cyan-dim/20">
                  <td className="py-2"><WalletChip address={p.wallet} showLink={false} /></td>
                  <td className="py-2 text-cream">{p.label}</td>
                  <td className="py-2 text-right text-steel">
                    {p.holder_pct != null ? `${p.holder_pct.toFixed(1)}%` : '—'}
                  </td>
                  <td className="py-2 text-right text-cream">{p.buys_sol.toFixed(3)}</td>
                  <td className="py-2 text-right text-cream">{p.sells_sol.toFixed(3)}</td>
                  <td className={`py-2 text-right ${pnlColorClass(p.realized_pnl_sol)}`}>
                    {fmtSol(p.realized_pnl_sol)}
                  </td>
                  <td className={`py-2 text-right ${pnlColorClass((p.roi_pct ?? 0) > 0 ? 1 : -1)}`}>
                    {fmtPct(p.roi_pct)}
                  </td>
                  <td className="py-2 text-right text-steel">{p.still_holds_pct.toFixed(0)}%</td>
                  <td className="py-2">
                    {p.behavior_tags.length > 0 ? (
                      <div className="flex flex-col gap-1">
                        {p.behavior_tags.map((t) => (
                          <span key={t} className={`tag-pill ${behClass(t)}`}>{t}</span>
                        ))}
                        {p.behavior_reason && (
                          <span className="text-steel text-[10px] italic">{p.behavior_reason}</span>
                        )}
                      </div>
                    ) : (
                      <span className="text-steel">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}