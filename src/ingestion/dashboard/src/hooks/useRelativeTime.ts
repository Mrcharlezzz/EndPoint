import { useEffect, useState } from 'react';

const MINUTE = 60_000;

/**
 * Format an ISO timestamp as a short relative time ("3m ago", "1h ago").
 * Re-renders once a minute while the component is mounted.
 */
export function useRelativeTime(iso: string | null | undefined): string {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => tick((n) => n + 1), MINUTE);
    return () => window.clearInterval(id);
  }, []);
  if (iso === null || iso === undefined || iso === '') return '—';
  return formatRelative(iso);
}

export function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const diffSec = Math.round((Date.now() - then) / 1000);
  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  const diffMo = Math.round(diffDay / 30);
  return `${diffMo}mo ago`;
}
