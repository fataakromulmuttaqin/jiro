/**
 * Reusable wallet address chip — monospace, clickable to Solscan.
 */

import { shortAddr, solscanWallet } from '../data';

interface WalletChipProps {
  address: string;
  label?: string;
  showLink?: boolean;
}

export function WalletChip({ address, label, showLink = true }: WalletChipProps) {
  return (
    <div className="inline-flex items-center gap-2">
      {label && <span className="font-mono text-xs text-cream">{label}</span>}
      {showLink ? (
        <a
          href={solscanWallet(address)}
          target="_blank"
          rel="noopener noreferrer"
          className="wallet-addr hover:text-cyan transition-colors"
          title={address}
        >
          {shortAddr(address)}
        </a>
      ) : (
        <span className="wallet-addr" title={address}>{shortAddr(address)}</span>
      )}
    </div>
  );
}