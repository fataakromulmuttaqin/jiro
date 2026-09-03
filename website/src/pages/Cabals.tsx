/**
 * Cabals page — all detected cabal clusters across all mints.
 * Priority: CABAL first, then SUSPECT_CLUSTER, SOLO skipped.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { fetchManifest, fetchReport, fmtSol, pnlColorClass, shortAddr } from '../data';
import type { CabalCluster, ReportManifest } from '../types';
import { WalletChip } from '../components/WalletChip';

interface ClusterWithMint extends CabalCluster {
  mint: string;
}

export function Cabals() {
  const [manifest, setManifest] = useState<ReportManifest | null>(null);
  const [clusters, setClusters] = useState<ClusterWithMint[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchManifest().then(async (m) => {
      if (!m) { setLoading(false); return; }
      setManifest(m);
      const reports = await Promise.all(m.mints.map((mi) => fetchReport(mi.mint.slice(0, 8))));
      const all: ClusterWithMint[] = [];
      for (const r of reports) {
        if (!r?.cabal) continue;
        for (const c of r.cabal.clusters) {
          if (c.type !== 'SOLO') all.push({ ...c, mint: r.mint });
        }
      }
      all.sort((a, b) => {
        if (a.type !== b.type) return a.type === 'CABAL' ? -1 : 1;
        return b.cabal_score - a.cabal_score;
      });
      setClusters(all);
      setLoading(false);
    });
  }, []);

  if (loading) return <div className="p-8 font-mono text-sm text-steel">Loading…</div>;
  if (!manifest) return <div className="p-8 font-mono text-sm text-steel">No data available.</div>;

  const cabals = clusters.filter((c) => c.type === 'CABAL');
  const suspects = clusters.filter((c) => c.type === 'SUSPECT_CLUSTER');

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="font-display text-2xl text-cream tracking-wider">CABALS</h1>
        <p className="font-mono text-xs text-steel mt-2">
          Shared-funder + co-buy clusters. Top signals first.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-8">
        <div className="stat-card p-5">
          <div className="font-mono text-xs text-steel uppercase">CABAL clusters</div>
          <div className="font-display text-3xl text-magenta mt-2">{cabals.length}</div>
        </div>
        <div className="stat-card p-5">
          <div className="font-mono text-xs text-steel uppercase">SUSPECT clusters</div>
          <div className="font-display text-3xl text-amber mt-2">{suspects.length}</div>
        </div>
      </div>

      {clusters.length === 0 ? (
        <div className="stat-card p-8 text-center font-mono text-sm text-steel">
          No cabal clusters detected yet. Run the pipeline on more mints.
        </div>
      ) : (
        <div className="space-y-4">
          {clusters.map((c) => (
            <div key={`${c.mint}-${c.cluster_id}`} className="stat-card p-5">
              <div className="flex items-center gap-3 mb-3 flex-wrap">
                <span className={`tag-pill ${c.type === 'CABAL' ? 'cabal' : 'suspect'}`}>
                  {c.type}
                </span>
                <span className="font-mono text-xs text-steel">
                  score {c.cabal_score.toFixed(2)}
                </span>
                {c.shared_funder && (
                  <span className="font-mono text-xs text-steel">
                    funder: <WalletChip address={c.shared_funder} showLink={false} />
                    {c.shared_funder_name && (
                      <span className="text-magenta ml-1">({c.shared_funder_name})</span>
                    )}
                  </span>
                )}
                <Link
                  to={`/mint/${shortAddr(c.mint, 8, 0)}`}
                  className="font-mono text-xs text-cyan hover:text-emerald ml-auto"
                >
                  mint {shortAddr(c.mint, 4, 4)} →
                </Link>
              </div>

              <div className="font-mono text-xs text-steel italic mb-3">
                {c.reason}
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {c.wallets.map((w) => (
                  <div key={w.wallet}
                       className="flex items-center justify-between px-3 py-2 bg-bg-primary rounded">
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
      )}
    </div>
  );
}