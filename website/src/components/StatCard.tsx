/**
 * StatCard — reusable glassmorphism card for top-level stats.
 */

interface StatCardProps {
  label: string;
  value: string | number;
  hint?: string;
  accent?: 'cyan' | 'emerald' | 'amber' | 'magenta' | 'red' | 'steel';
}

const ACCENT_COLORS: Record<string, string> = {
  cyan: 'text-cyan',
  emerald: 'text-emerald',
  amber: 'text-amber',
  magenta: 'text-magenta',
  red: 'text-red',
  steel: 'text-steel',
};

export function StatCard({ label, value, hint, accent = 'cyan' }: StatCardProps) {
  return (
    <div className="stat-card p-5">
      <div className="font-mono text-xs text-steel tracking-wider uppercase">
        {label}
      </div>
      <div className={`font-display text-3xl mt-2 ${ACCENT_COLORS[accent]}`}>
        {value}
      </div>
      {hint && (
        <div className="font-mono text-xs text-steel mt-1">{hint}</div>
      )}
    </div>
  );
}