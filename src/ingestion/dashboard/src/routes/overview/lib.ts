import type { SyncHistoryItem } from '@/api/queries';

export type HeatTone = 'ok' | 'warn' | 'err' | 'idle';

export interface HeatCellData {
  tone: HeatTone;
  dim?: 1 | 2 | 3 | undefined;
  count: number;
  failed: number;
  label: string;
}

// Every content platform the daemon knows about. Used by the per-platform
// aggregate view (PlatformPanel), which is NOT per-user — Telegram belongs
// here because its sync health/latency/errors matter at the platform level
// even though the credential itself is owned by the bot. Per-user UI
// filters the bot separately via ``TELEGRAM_BOT_EMAIL``.
const PLATFORMS = ['teams', 'moodle', 'telegram'] as const;

// Sentinel email that owns the Telegram bot's PlatformCredential row.
// Must stay in sync with ``daemon.system_users.TELEGRAM_BOT_USER_EMAIL``.
export const TELEGRAM_BOT_EMAIL = 'telegram-bot@system.endpoint';

/**
 * Human label for a user_email. Remaps the bot sentinel to "Telegram bot"
 * so feed rows don't surface the raw sentinel string. Plain user emails
 * get their local-part displayed (the feeds don't need the @domain).
 */
export function displayUserLabel(email: string | null | undefined): string {
  if (email === TELEGRAM_BOT_EMAIL) return 'Telegram bot';
  if (typeof email === 'string' && email.length > 0) {
    return email.split('@')[0] ?? email;
  }
  return '—';
}
export type KnownPlatform = (typeof PLATFORMS)[number];
export const KNOWN_PLATFORMS: readonly KnownPlatform[] = PLATFORMS;

/**
 * Bucket sync history into a 24h × per-platform heatmap.
 * Hour 0 = 23h ago, hour 23 = the current hour.
 */
export function bucketHeatmap(
  syncs: readonly SyncHistoryItem[],
  now: Date = new Date(),
): Record<string, HeatCellData[]> {
  const out: Record<string, HeatCellData[]> = {};
  const nowMs = now.getTime();
  for (const p of PLATFORMS) {
    out[p] = Array.from({ length: 24 }, () => ({
      tone: 'idle' as const,
      count: 0,
      failed: 0,
      label: '',
    }));
  }
  for (const s of syncs) {
    const plat = s.platform;
    const row = out[plat];
    if (row === undefined) continue;
    const t = s.started_at !== null ? new Date(s.started_at).getTime() : NaN;
    if (Number.isNaN(t)) continue;
    const hoursAgo = Math.floor((nowMs - t) / (60 * 60 * 1000));
    if (hoursAgo < 0 || hoursAgo > 23) continue;
    const bucket = 23 - hoursAgo;
    const cell = row[bucket];
    if (cell === undefined) continue;
    cell.count += 1;
    if (s.state === 'failed' || s.error !== null) cell.failed += 1;
  }
  for (const p of PLATFORMS) {
    const row = out[p];
    if (row === undefined) continue;
    out[p] = row.map((cell, i) => classify(cell, p, i));
  }
  return out;
}

function classify(cell: HeatCellData, plat: string, hour: number): HeatCellData {
  const label = `${plat} · hour ${String(hour).padStart(2, '0')} · ${cell.count} runs · ${cell.failed} failed`;
  if (cell.count === 0) return { ...cell, tone: 'idle', label };
  const failRate = cell.failed / cell.count;
  if (failRate >= 0.5) return { ...cell, tone: 'err', label };
  if (failRate >= 0.1) return { ...cell, tone: 'warn', label };
  // green intensity by activity volume (more runs = more saturated)
  const dim: 1 | 2 | 3 | undefined =
    cell.count >= 8 ? undefined : cell.count >= 4 ? 1 : cell.count >= 2 ? 2 : 3;
  return { ...cell, tone: 'ok', dim, label };
}

/** Classify sync state for the activity feed chip. */
export function syncTone(state: string): 'ok' | 'run' | 'err' | 'warn' | 'idle' {
  // Daemon writes 'complete' (STATE_COMPLETE in ports/sync_logger.py).
  // 'completed' is kept as an alias for events that were already routed
  // through an older code path; removing it would re-regress.
  if (state === 'complete' || state === 'completed') return 'ok';
  if (state === 'running') return 'run';
  if (state === 'failed') return 'err';
  if (state === 'cancelled') return 'warn';
  return 'idle';
}

/** Format HH:MM:SS.mmm from an ISO timestamp (or '—'). */
export function formatClock(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
}

/** Format a sync's duration as a short string. */
export function formatDuration(startedAt: string | null, finishedAt: string | null): string {
  if (startedAt === null || finishedAt === null) return '—';
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(ms) || ms < 0) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function itemsFromStats(stats: unknown): number | null {
  if (stats === null || typeof stats !== 'object') return null;
  const rec = stats as Record<string, unknown>;
  const fetched = rec['items_fetched'] ?? rec['items_new'] ?? rec['items'];
  return typeof fetched === 'number' ? fetched : null;
}
