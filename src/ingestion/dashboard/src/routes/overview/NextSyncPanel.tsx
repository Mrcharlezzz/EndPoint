import { Fragment, useEffect, useMemo, useState, type ReactElement } from 'react';
import { Card, CardBody, CardHeader, Chip, Icon, PlatformChip } from '@/components/ui';
import { useForceSync, useScheduleNext } from '@/api/queries';
import { getErrorMessage } from '@/lib/errors';
import { toast } from '@/lib/toasts';
import { cn } from '@/lib/cn';
import { capitalize } from './lib';
import type { components } from '@/api/schema';
import type { SyncHistoryItem } from '@/api/queries';

type NextItem = components['schemas']['NextSyncItem'];
type BlockedBy = NonNullable<NextItem['blocked_by']>;

const BLOCKED_LABEL: Record<BlockedBy, string> = {
  emergency_stop: 'emergency stop',
  drain_mode: 'draining',
  global_paused: 'all paused',
  platform_disabled: 'platform off',
  platform_paused: 'platform paused',
  expired: 'expired',
  paused: 'paused',
  suppressed: 'suppressed',
};

const BLOCKED_TONE: Record<BlockedBy, 'err' | 'warn' | 'idle'> = {
  emergency_stop: 'err',
  drain_mode: 'warn',
  global_paused: 'warn',
  platform_disabled: 'err',
  platform_paused: 'warn',
  expired: 'err',
  paused: 'idle',
  suppressed: 'warn',
};

interface NextSyncPanelProps {
  // Rolling-average duration lookup is derived from recent sync history —
  // passed in so the panel doesn't need its own query.
  syncs: readonly SyncHistoryItem[];
}

export function NextSyncPanel({ syncs }: NextSyncPanelProps): ReactElement {
  const query = useScheduleNext();
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const avgDurationMs = useMemo(() => rollingAvgDurationByPlatform(syncs), [syncs]);

  const grouped = useMemo(() => {
    const items = query.data?.items ?? [];
    const byPlatform = new Map<string, NextItem[]>();
    for (const item of items) {
      const bucket = byPlatform.get(item.platform) ?? [];
      bucket.push(item);
      byPlatform.set(item.platform, bucket);
    }
    const platforms = Array.from(byPlatform.keys()).sort();
    return platforms.map((p) => ({
      platform: p,
      items: (byPlatform.get(p) ?? []).sort(sortItems),
    }));
  }, [query.data]);

  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const toggleCollapse = (plat: string): void => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(plat)) next.delete(plat);
      else next.add(plat);
      return next;
    });
  };

  const total = grouped.reduce((n, g) => n + g.items.length, 0);

  return (
    <Card className="feed-card">
      <CardHeader
        right={<span className="sub">{query.isLoading ? 'loading…' : `${total} credentials`}</span>}
      >
        <span className="title">Next sync</span>
      </CardHeader>
      <CardBody flush className="feed-body">
        <table className="t">
          <thead>
            <tr>
              <th>user</th>
              <th style={{ width: 80 }}>plat</th>
              <th className="right" style={{ width: 110 }}>
                interval
              </th>
              <th className="right" style={{ width: 160 }}>
                next in
              </th>
              <th style={{ width: 40 }} />
            </tr>
          </thead>
          <tbody>
            {grouped.length === 0 && (
              <tr>
                <td colSpan={5} className="dim tiny" style={{ padding: '12px 16px' }}>
                  {query.isLoading ? 'Loading…' : 'No credentials registered.'}
                </td>
              </tr>
            )}
            {grouped.map((g) => {
              const isCollapsed = collapsed.has(g.platform);
              const runnable = g.items.filter((it) => it.blocked_by === null).length;
              return (
                <Fragment key={g.platform}>
                  <tr className="nsp-section-head">
                    <td colSpan={5}>
                      <button
                        type="button"
                        className="nsp-section-toggle"
                        aria-expanded={!isCollapsed}
                        onClick={() => toggleCollapse(g.platform)}
                      >
                        <span
                          className={cn('group-chevron', !isCollapsed && 'open')}
                          aria-hidden="true"
                        >
                          <Icon name="chevron-down" size={12} />
                        </span>
                        <PlatformChip name={g.platform} />
                        <span className="mono tiny dim">
                          {g.items.length} ·{' '}
                          <span className={runnable > 0 ? undefined : 'dim'}>
                            {runnable} runnable
                          </span>
                        </span>
                      </button>
                    </td>
                  </tr>
                  {!isCollapsed &&
                    g.items.map((item) => (
                      <NextRow
                        key={`${item.user_email}::${item.platform}`}
                        item={item}
                        nowMs={nowMs}
                        avgMs={avgDurationMs[item.platform]}
                      />
                    ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

function NextRow({
  item,
  nowMs,
  avgMs,
}: {
  item: NextItem;
  nowMs: number;
  avgMs: number | undefined;
}): ReactElement {
  const forceSync = useForceSync();
  const userShort = item.user_email.split('@')[0];
  const canForce = !item.running && item.blocked_by === null;
  const busy = forceSync.isPending;

  async function onForce(): Promise<void> {
    // ``disabled={busy}`` only reflects after React paints; a second click
    // fired in the same event-loop tick would sneak past. Guard up-front
    // against the live mutation-state.
    if (forceSync.isPending) return;
    try {
      await forceSync.mutateAsync({
        email: item.user_email,
        platform: item.platform,
        reason: 'Overview · sync now',
      });
      toast.success('Sync queued', `${userShort} · ${capitalize(item.platform)}`);
    } catch (err: unknown) {
      toast.error('Sync failed', getErrorMessage(err));
    }
  }

  return (
    <tr>
      <td
        className="pri"
        style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)' }}
        title={item.user_email}
      >
        {userShort}
      </td>
      <td>
        <PlatformChip name={item.platform} />
      </td>
      <td className="num dim">{formatInterval(item.interval_seconds)}</td>
      <td className="num">
        <RunState item={item} nowMs={nowMs} avgMs={avgMs} />
      </td>
      <td>
        {canForce && (
          <button
            type="button"
            className="nsp-force"
            aria-label={`Sync ${userShort} ${item.platform} now`}
            disabled={busy}
            onClick={() => void onForce()}
          >
            <Icon name="refresh" size={12} />
          </button>
        )}
      </td>
    </tr>
  );
}

function RunState({
  item,
  nowMs,
  avgMs,
}: {
  item: NextItem;
  nowMs: number;
  avgMs: number | undefined;
}): ReactElement {
  if (item.running) {
    // ``running_started_at`` is not in the schema; we only know the sync is
    // active. Show an indeterminate progress bar unless we have an avg
    // duration — then render a shimmer gated to avg-time width.
    if (avgMs !== undefined && avgMs > 0) {
      return (
        <span className="nsp-running" title={`avg ${Math.round(avgMs / 1000)}s`}>
          <Chip tone="run">running</Chip>
          <span className="nsp-running-bar" aria-hidden="true">
            <span className="nsp-running-fill" style={{ animationDuration: `${avgMs}ms` }} />
          </span>
        </span>
      );
    }
    return <Chip tone="run">running</Chip>;
  }
  if (item.blocked_by !== null) {
    return <Chip tone={BLOCKED_TONE[item.blocked_by]}>{BLOCKED_LABEL[item.blocked_by]}</Chip>;
  }
  const dueMs = Date.parse(item.next_due_at);
  if (Number.isNaN(dueMs)) {
    return <span className="dim">—</span>;
  }
  const remaining = Math.round((dueMs - nowMs) / 1000);
  if (remaining <= 0) {
    return <Chip tone="run">due now</Chip>;
  }
  return <span className="mono">in {formatRemaining(remaining)}</span>;
}

function sortItems(a: NextItem, b: NextItem): number {
  const aBlocked = a.blocked_by !== null;
  const bBlocked = b.blocked_by !== null;
  if (aBlocked !== bBlocked) return aBlocked ? 1 : -1;
  if (aBlocked) return a.user_email.localeCompare(b.user_email);
  return a.next_due_at.localeCompare(b.next_due_at);
}

function rollingAvgDurationByPlatform(syncs: readonly SyncHistoryItem[]): Record<string, number> {
  // Use only completed syncs with both timestamps. 10 most recent per
  // platform, averaged. Enough signal for a running-row ETA without over-
  // weighting freak-slow or cached-fast syncs.
  const samples = new Map<string, number[]>();
  for (const s of syncs) {
    // Daemon writes 'complete'; accept 'completed' as an alias to match the
    // same belt-and-suspenders pattern used in lib.ts::syncTone for WS rows
    // that passed through older mappings.
    if (s.state !== 'complete' && s.state !== 'completed') continue;
    if (s.started_at === null || s.finished_at === null) continue;
    const start = new Date(s.started_at).getTime();
    const end = new Date(s.finished_at).getTime();
    if (Number.isNaN(start) || Number.isNaN(end) || end <= start) continue;
    const arr = samples.get(s.platform) ?? [];
    if (arr.length < 10) arr.push(end - start);
    samples.set(s.platform, arr);
  }
  const out: Record<string, number> = {};
  for (const [plat, arr] of samples) {
    if (arr.length === 0) continue;
    out[plat] = arr.reduce((acc, v) => acc + v, 0) / arr.length;
  }
  return out;
}

function formatRemaining(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (minutes < 60) return `${minutes}:${String(secs).padStart(2, '0')}`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${hours}h ${String(mins).padStart(2, '0')}m`;
}

function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 360) / 10}h`;
}
