/**
 * Sidebar navigation — cyberpunk style matching dashboard-development skill.
 */

import { NavLink } from 'react-router-dom';

const NAV_ITEMS = [
  { path: '/', label: 'OVERVIEW', icon: '◎' },
  { path: '/wallets', label: 'WALLETS', icon: '◈' },
  { path: '/cabals', label: 'CABALS', icon: '◇' },
  { path: '/watchlist', label: 'WATCHLIST', icon: '◉' },
  { path: '/behavior', label: 'BEHAVIOR', icon: '◆' },
];

export function Sidebar() {
  return (
    <nav className="w-64 min-h-screen flex flex-col border-r border-cyan-dim">
      <div className="p-6 border-b border-cyan-dim">
        <div className="font-display text-2xl tracking-wider text-cyan">
          JIRO
        </div>
        <div className="font-display text-lg tracking-wider text-emerald">
          SNIPER NET
        </div>
        <div className="font-mono text-xs text-steel mt-2">
          cabal · grinder · tracker
        </div>
        <div className="flex items-center gap-2 mt-3">
          <div className="w-2 h-2 rounded-full bg-emerald animate-pulse" />
          <span className="font-mono text-xs text-emerald">SYSTEM ONLINE</span>
        </div>
      </div>

      <div className="flex-1 py-4">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            end={item.path === '/'}
            className={({ isActive }: { isActive: boolean }) =>
              `block px-6 py-3 font-mono text-sm tracking-wider transition-colors ${
                isActive
                  ? 'bg-cyan-dim text-cyan border-l-2 border-cyan'
                  : 'text-cream opacity-70 hover:opacity-100 hover:bg-cyan-dim border-l-2 border-transparent'
              }`
            }
          >
            <span className="mr-3 text-cyan">{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </div>

      <div className="p-4 border-t border-cyan-dim font-mono text-xs text-steel">
        <div>Data: Helius RPC · Solana</div>
        <div className="mt-1">$0/month · 100% free</div>
      </div>
    </nav>
  );
}