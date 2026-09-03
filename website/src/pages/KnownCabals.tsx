/**
 * Known Cabals page — displays the user-curated cabal seed DB.
 * Shows all funder addresses + their cabal names from cabal_seeds.json.
 */

import { useEffect, useState } from 'react';
import { fetchCabalSeeds, shortAddr, solscanWallet } from '../data';

export function KnownCabals() {
  const [seeds, setSeeds] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchCabalSeeds().then((s) => {
      setSeeds(s);
      setLoading(false);
    });
  }, []);

  if (loading) return <div className="p-8 font-mono text-sm text-steel">Loading…</div>;

  const entries = Object.entries(seeds).sort((a, b) => a[1].localeCompare(b[1]));

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="font-display text-2xl text-cream tracking-wider">KNOWN CABALS</h1>
        <p className="font-mono text-xs text-steel mt-2">
          Curated funder addresses. When detected as shared_funder, cluster gets +0.3 score
          boost + the cabal name.
        </p>
      </div>

      <div className="stat-card p-6 mb-6 font-mono text-xs text-steel">
        Add entries to <code className="text-cyan">cabal_seeds.json</code> at the project
        root, then run <code className="text-cyan">python3 sync_website_data.py</code> and
        push to master.
      </div>

      {entries.length === 0 ? (
        <div className="stat-card p-8 text-center">
          <div className="font-mono text-sm text-steel mb-3">
            No cabal seeds configured yet. Cold start.
          </div>
          <div className="font-mono text-xs text-steel">
            Once you add entries to <code className="text-cyan">cabal_seeds.json</code>,
            they'll appear here and boost detection scores.
          </div>
        </div>
      ) : (
        <div className="stat-card p-6">
          <table className="w-full font-mono text-sm">
            <thead>
              <tr className="text-steel uppercase tracking-wider text-xs border-b border-cyan-dim">
                <th className="text-left py-2">Cabal name</th>
                <th className="text-left py-2">Funder address</th>
                <th className="text-right py-2">Solscan</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([addr, name]) => (
                <tr key={addr} className="border-b border-cyan-dim/30 hover:bg-cyan-dim/20">
                  <td className="py-3 text-magenta">{name}</td>
                  <td className="py-3 text-cyan">
                    <a href={solscanWallet(addr)}
                       target="_blank" rel="noopener noreferrer"
                       className="hover:text-emerald">
                      {shortAddr(addr, 6, 6)}
                    </a>
                  </td>
                  <td className="py-3 text-right">
                    <a href={solscanWallet(addr)}
                       target="_blank" rel="noopener noreferrer"
                       className="text-cyan hover:text-emerald text-xs">
                      ↗
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}