import type { SyncHistoryItem } from '@/api/queries';
import type { ChipTone } from '@/components/ui';

export type SyncState = string;

export interface StateFilter {
  label: string;
  value: string;
}

export const STATE_FILTERS: readonly StateFilter[] = [
  { label: 'running', value: 'running' },
  { label: 'completed', value: 'completed' },
  { label: 'failed', value: 'failed' },
  { label: 'cancelled', value: 'cancelled' },
] as const;

export function stateTone(state: string): ChipTone {
  if (state === 'completed') return 'ok';
  if (state === 'running') return 'run';
  if (state === 'failed') return 'err';
  if (state === 'cancelled') return 'warn';
  return 'idle';
}

export function stateLabel(state: string): string {
  if (state === 'completed') return 'ok';
  if (state === 'running') return 'run';
  if (state === 'failed') return 'err';
  return state;
}

/** Count syncs in each state bucket for the filter pills. */
export function countByState(syncs: readonly SyncHistoryItem[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const s of syncs) out[s.state] = (out[s.state] ?? 0) + 1;
  return out;
}

/** Free-text filter: matches user email, sync id, platform, or error substring. */
export function matchesQuery(s: SyncHistoryItem, q: string): boolean {
  if (q.length === 0) return true;
  const needle = q.toLowerCase();
  return (
    s.user_email.toLowerCase().includes(needle) ||
    s.id.toLowerCase().includes(needle) ||
    s.platform.toLowerCase().includes(needle) ||
    (s.error?.toLowerCase().includes(needle) ?? false)
  );
}

export function itemsFetched(stats: unknown): number {
  if (stats === null || typeof stats !== 'object') return 0;
  const rec = stats as Record<string, unknown>;
  const v = rec['items_fetched'] ?? rec['items_new'];
  return typeof v === 'number' ? v : 0;
}

export function durationMs(startedAt: string | null, finishedAt: string | null): number {
  if (startedAt === null || finishedAt === null) return 0;
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  return Number.isNaN(ms) || ms < 0 ? 0 : ms;
}

export function formatSeconds(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Format HH:MM:SS (no ms) for compact table cells. */
export function formatClockShort(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}
