/**
 * Overview / Dashboard — summary cards + latest mint table.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { fetchBatch, fetchManifest, formatTs, shortAddr } from '../data';
import type { BatchSummary as BatchSummaryT, ReportManifest } from '../types';
import { StatCard } from '../components/StatCard';

export function Overview() {
  const [batch, setBatch] = useState<BatchSummaryT | null>(null);
  const [manifest, setManifest] = useState<ReportManifest | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([fetchBatch(), fetchManifest()]).then(([b, m]) => {
      setBatch(b);
      setManifest(m);
      setLoading(false);
    });
  }, []);

  const mints = manifest?.mints ?? [];
  const totalCabal = batch?.reports.reduce((acc, r) => acc + (r.summary?.n_cabal ?? 0), 0) ?? 0;
  const totalSuspect = batch?.reports.reduce((acc, r) => acc + (r.summary?.n_suspect ?? 0), 0) ?? 0;
  const totalWinners = batch?.reports.reduce((acc, r) => acc + r.n_winners, 0) ?? 0;
  const totalLosers = batch?.reports.reduce((acc, r) => acc + r.n_losers, 0) ?? 0;

  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="font-display text-3xl text-cream tracking-wider">OVERVIEW</h1>
        <p className="font-mono text-xs text-steel mt-2">
          Latest sniper net run · {mints.length} mints tracked
        </p>
      </div>

      {loading ? (
        <div className="font-mono text-sm text-steel">Loading…</div>
      ) : !manifest || mints.length === 0 ? (
        <div className="stat-card p-8 text-center">
          <div className="font-mono text-steel text-sm">
            No data yet. Run the pipeline:
          </div>
          <pre className="font-mono text-xs text-cyan mt-3 inline-block text-left">
            cd ~/ruangkerja/jiro && python3 run_sniper_net.py &lt;MINT&gt;
          </pre>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <StatCard label="Mints tracked" value={mints.length} accent="cyan" />
            <StatCard label="Cabals" value={totalCabal} accent="magenta"
                       hint="shared funder + co-buy" />
            <StatCard label="Suspect" value={totalSuspect} accent="amber" />
            <StatCard label="Winners / Losers"
                       value={`${totalWinners} / ${totalLosers}`}
                       accent="emerald" />
          </div>

          <div className="stat-card p-6">
            <h2 className="font-display text-xl text-cream mb-4">Tracked Mints</h2>
            <table className="w-full font-mono text-sm">
              <thead>
                <tr className="text-steel text-xs uppercase tracking-wider border-b border-cyan-dim">
                  <th className="text-left py-2">Mint</th>
                  <th className="text-right py-2">Holders</th>
                  <th className="text-right py-2">Cabals</th>
                  <th className="text-right py-2">Winners</th>
                  <th className="text-right py-2">Analyzed</th>
                  <th className="text-right py-2"></th>
                </tr>
              </thead>
              <tbody>
                {mints.map((m) => (
                  <tr key={m.mint} className="border-b border-cyan-dim/40 hover:bg-cyan-dim/30">
                    <td className="py-3 text-cyan">{shortAddr(m.mint, 6, 6)}</td>
                    <td className="py-3 text-right text-cream">{m.n_holders}</td>
                    <td className="py-3 text-right">
                      {m.n_cabal > 0 ? (
                        <span className="text-magenta">{m.n_cabal}</span>
                      ) : (
                        <span className="text-steel">0</span>
                      )}
                    </td>
                    <td className="py-3 text-right text-emerald">{m.n_winners}</td>
                    <td className="py-3 text-right text-steel text-xs">
                      {formatTs(m.analyzed_at)}
                    </td>
                    <td className="py-3 text-right">
                      <Link
                        to={`/mint/${shortAddr(m.mint, 8, 0)}`}
                        className="text-cyan hover:text-emerald"
                      >
                        →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}