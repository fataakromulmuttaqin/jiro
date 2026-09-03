/**
 * Watchlist page — auto-curated winners from Jiro Smart Money.
 * Reads /data/watchlist.json + /data/watchlist_diff.json (diff log).
 */

import { useEffect, useState } from 'react';
import { shortAddr, formatTs, solscanWallet } from '../data';

interface WatchEntry {
  address: string;
  label: string;
  added_ts: number;
  source: string;
  added_via_mint?: string;
}

interface WatchDiff {
  ts: number;
  added: WatchEntry[];
  pruned: Array<{ address: string; label: string }>;
  final_size: number;
  mint?: string;
}

export function Watchlist() {
  const [list, setList] = useState<WatchEntry[]>([]);
  const [diffs, setDiffs] = useState<WatchDiff[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch('/data/watchlist.json').then((r) => r.ok ? r.json() : []).catch(() => []),
      fetch('/data/watchlist_diff.json').then((r) => r.ok ? r.json() : []).catch(() => []),
    ]).then(([l, d]) => {
      setList(l);
      setDiffs(d.reverse());  // newest first
      setLoading(false);
    });
  }, []);

  if (loading) return <div className="p-8 font-mono text-sm text-steel">Loading…</div>;

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="font-display text-2xl text-cream tracking-wider">WATCHLIST</h1>
        <p className="font-mono text-xs text-steel mt-2">
          Auto-curated winners · fed into smart_money.py for convergence signals
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Current list */}
        <div className="stat-card p-6">
          <h2 className="font-display text-xl text-cream mb-4">
            CURRENT LIST <span className="text-cyan font-mono text-sm">({list.length})</span>
          </h2>
          {list.length === 0 ? (
            <div className="font-mono text-sm text-steel">
              Empty. Will populate as winners are detected.
            </div>
          ) : (
            <div className="space-y-2">
              {list.map((w) => (
                <div key={w.address}
                     className="flex items-center justify-between px-3 py-2 bg-bg-primary rounded">
                  <div className="flex flex-col">
                    <a href={solscanWallet(w.address)}
                       target="_blank" rel="noopener noreferrer"
                       className="wallet-addr text-cyan hover:text-emerald">
                      {shortAddr(w.address)}
                    </a>
                    <span className="font-mono text-xs text-cream opacity-80">{w.label}</span>
                  </div>
                  <div className="text-right">
                    <span className={`tag-pill ${w.source === 'sniper_net' ? 'suspect' : 'solo'}`}>
                      {w.source}
                    </span>
                    <div className="font-mono text-[10px] text-steel mt-1">
                      {formatTs(w.added_ts)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Diff log */}
        <div className="stat-card p-6">
          <h2 className="font-display text-xl text-cream mb-4">
            RECENT CHANGES <span className="text-steel font-mono text-sm">({diffs.length})</span>
          </h2>
          {diffs.length === 0 ? (
            <div className="font-mono text-sm text-steel">
              No changes yet.
            </div>
          ) : (
            <div className="space-y-3 max-h-[600px] overflow-y-auto">
              {diffs.map((d, i) => (
                <div key={i} className="border-b border-cyan-dim/30 pb-3">
                  <div className="font-mono text-xs text-steel">
                    {formatTs(d.ts)}
                    {d.mint && (
                      <span className="ml-2 text-cyan">mint {shortAddr(d.mint, 4, 4)}</span>
                    )}
                  </div>
                  {d.added.length > 0 && (
                    <div className="mt-1">
                      <span className="font-mono text-xs text-emerald">+ added:</span>
                      {d.added.map((a) => (
                        <a key={a.address} href={solscanWallet(a.address)}
                           target="_blank" rel="noopener noreferrer"
                           className="ml-2 wallet-addr text-cyan hover:text-emerald">
                          {shortAddr(a.address)}
                        </a>
                      ))}
                    </div>
                  )}
                  {d.pruned.length > 0 && (
                    <div className="mt-1">
                      <span className="font-mono text-xs text-red">- pruned:</span>
                      {d.pruned.map((p) => (
                        <span key={p.address} className="ml-2 wallet-addr">
                          {shortAddr(p.address)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}