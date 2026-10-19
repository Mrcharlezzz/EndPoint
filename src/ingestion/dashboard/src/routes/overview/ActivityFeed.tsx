import { Fragment, useMemo, useRef, useState, type ReactElement } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Icon,
  PhaseTrack,
  PlatformChip,
  type PhaseSegment,
} from '@/components/ui';
import { LiveChip } from '@/components/LiveChip';
import { useChannel } from '@/ws/useChannel';
import { useSync } from '@/api/queries';
import { displayUserLabel, formatClock, formatDuration, itemsFromStats, syncTone } from './lib';
import { cn } from '@/lib/cn';
import type { SyncHistoryItem } from '@/api/queries';

interface ActivityRow {
  id: string;
  started_at: string | null;
  finished_at: string | null;
  state: string;
  user_email: string;
  platform: string;
  stats: unknown;
  _new?: boolean;
}

interface ActivityFeedProps {
  initial: readonly SyncHistoryItem[];
  limit?: number;
}

export function ActivityFeed({ initial, limit = 50 }: ActivityFeedProps): ReactElement {
  // Rows from the WS stream. New events prepend; old rows fall off after `limit`.
  const [wsRows, setWsRows] = useState<ActivityRow[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const streamIdsRef = useRef<Set<string>>(new Set());

  useChannel('/ws/syncs', (e) => {
    const eventName = typeof e.event === 'string' ? e.event : '';
    const userEmail = typeof e.user_email === 'string' ? e.user_email : '';
    const platform = typeof e.platform === 'string' ? e.platform : '';
    const id =
      typeof e.sync_id === 'string' && e.sync_id.length > 0
        ? e.sync_id
        : `ws:${userEmail}:${platform}`;
    streamIdsRef.current.add(id);
    const next: ActivityRow = {
      id,
      user_email: userEmail,
      platform,
      started_at:
        typeof e['started_at'] === 'string'
          ? e['started_at']
          : eventName === 'sync.started'
            ? new Date().toISOString()
            : null,
      finished_at:
        typeof e['finished_at'] === 'string'
          ? e['finished_at']
          : eventName === 'sync.finished' || eventName === 'sync.failed'
            ? new Date().toISOString()
            : null,
      state:
        eventName === 'sync.started'
          ? 'running'
          : eventName === 'sync.finished'
            ? 'complete'
            : eventName === 'sync.failed'
              ? 'failed'
              : 'running',
      stats: e['stats'],
      _new: true,
    };
    setWsRows((prev) => {
      const without = prev.filter((r) => r.id !== id);
      return [next, ...without].slice(0, limit);
    });
  });

  const rows = useMemo<ActivityRow[]>(() => {
    const base: ActivityRow[] = initial.slice(0, limit).map((s) => ({
      id: s.id,
      started_at: s.started_at,
      finished_at: s.finished_at,
      state: s.state,
      user_email: s.user_email,
      platform: s.platform,
      stats: s.stats,
      _new: false,
    }));

    const initialPairs = new Set(base.map((r) => `${r.user_email}:${r.platform}`));
    const liveWsRows = wsRows.filter((r) => {
      if (!r.id.startsWith('ws:')) return true;
      return !initialPairs.has(`${r.user_email}:${r.platform}`);
    });

    const wsIds = new Set(liveWsRows.map((r) => r.id));
    const dedupedBase = base.filter((r) => !wsIds.has(r.id));
    return [...liveWsRows, ...dedupedBase].slice(0, limit);
  }, [initial, wsRows, limit]);

  // Items-per-row max lets the inline mini-bar scale against the window's
  // largest sync rather than against an arbitrary constant.
  const maxItems = useMemo(() => {
    let max = 0;
    for (const r of rows) {
      const v = itemsFromStats(r.stats);
      if (v !== null && v > max) max = v;
    }
    return max;
  }, [rows]);

  const [expandedId, setExpandedId] = useState<string | null>(null);

  return (
    <Card className="feed-card">
      <CardHeader
        title={<LiveChip />}
        right={
          <>
            <span className="sub">last {rows.length}</span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setAutoScroll((v) => !v)}
              aria-pressed={autoScroll}
            >
              {autoScroll ? 'auto-scroll on' : 'auto-scroll off'}
            </Button>
            <Button size="sm" variant="ghost" aria-label="Filter">
              <Icon name="filter" size={11} />
            </Button>
          </>
        }
      >
        <span className="title">Activity feed</span>
      </CardHeader>
      <CardBody flush className="feed-body">
        <table className="t">
          <thead>
            <tr>
              <th style={{ width: 90 }}>time</th>
              <th style={{ width: 60 }}>state</th>
              <th>user</th>
              <th style={{ width: 80 }}>plat</th>
              <th className="right" style={{ width: 60 }}>
                dur
              </th>
              <th className="right" style={{ width: 90 }}>
                items
              </th>
              <th style={{ width: 28 }} />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="dim tiny" style={{ padding: '12px 16px' }}>
                  No sync activity yet.
                </td>
              </tr>
            )}
            {rows.map((r, i) => {
              const tone = syncTone(r.state);
              const items = itemsFromStats(r.stats);
              const itemsPct = items !== null && maxItems > 0 ? (items / maxItems) * 100 : 0;
              const isWs = r.id.startsWith('ws:');
              const isRunning = r.state === 'running';
              const expandable = !isWs;
              const isExpanded = expandedId === r.id;
              const toggle = (): void => {
                setExpandedId((prev) => (prev === r.id ? null : r.id));
              };
              return (
                <Fragment key={r.id}>
                  <tr
                    className={cn(
                      'activity-row',
                      i === 0 && r._new === true && 'stream-row',
                      isRunning && 'activity-row-running',
                      expandable && 'activity-row-expandable',
                      isExpanded && 'activity-row-expanded',
                    )}
                    onClick={expandable ? toggle : undefined}
                    style={expandable ? { cursor: 'pointer' } : undefined}
                  >
                    <td className="mono">{formatClock(r.started_at)}</td>
                    <td>
                      <Chip tone={tone}>{r.state}</Chip>
                    </td>
                    <td
                      className="pri"
                      style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)' }}
                      title={r.user_email}
                    >
                      {displayUserLabel(r.user_email)}
                    </td>
                    <td>
                      <PlatformChip name={r.platform} />
                    </td>
                    <td className="num">{formatDuration(r.started_at, r.finished_at)}</td>
                    <td className="num">
                      <ItemsCell items={items} pct={itemsPct} tone={tone} />
                    </td>
                    <td className="group-toggle">
                      {expandable && (
                        <button
                          type="button"
                          className={cn('group-chevron', isExpanded && 'open')}
                          aria-expanded={isExpanded}
                          aria-label={`${isExpanded ? 'Collapse' : 'Expand'} sync detail`}
                          onClick={(e) => {
                            e.stopPropagation();
                            toggle();
                          }}
                        >
                          <Icon name="chevron-down" size={12} />
                        </button>
                      )}
                    </td>
                  </tr>
                  {isExpanded && <ExpandedRow syncId={r.id} />}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

function ExpandedRow({ syncId }: { syncId: string }): ReactElement {
  const detail = useSync(syncId);
  const segments = useMemo<PhaseSegment[]>(
    () => buildPhaseSegments(detail.data?.profile ?? null),
    [detail.data],
  );
  return (
    <tr className="activity-detail-row">
      <td colSpan={7}>
        {detail.isLoading && <span className="tiny dim">Loading detail…</span>}
        {detail.isError && <span className="tiny text-err">Detail unavailable.</span>}
        {detail.data !== undefined && segments.length === 0 && (
          <span className="tiny dim">No phase profile recorded for this sync.</span>
        )}
        {segments.length > 0 && (
          <div className="activity-phases">
            <PhaseTrack segments={segments} />
          </div>
        )}
      </td>
    </tr>
  );
}

function ItemsCell({
  items,
  pct,
  tone,
}: {
  items: number | null;
  pct: number;
  tone: ReturnType<typeof syncTone>;
}): ReactElement {
  if (items === null) return <span className="dim">—</span>;
  const barColor =
    tone === 'ok'
      ? 'var(--ok)'
      : tone === 'err'
        ? 'var(--err)'
        : tone === 'warn'
          ? 'var(--warn)'
          : 'var(--text-4)';
  return (
    <span className="items-cell">
      <span className="items-bar" aria-hidden="true">
        <span
          className="items-bar-fill"
          style={{ width: `${Math.min(100, pct).toFixed(1)}%`, background: barColor }}
        />
      </span>
      <span className="items-n">{items}</span>
    </span>
  );
}

function buildPhaseSegments(profile: unknown): PhaseSegment[] {
  if (profile === null || profile === undefined || typeof profile !== 'object') return [];
  const rec = profile as Record<string, unknown>;
  const phases = rec['phases'];
  if (!Array.isArray(phases)) return [];
  const out: PhaseSegment[] = [];
  for (const p of phases) {
    if (p === null || typeof p !== 'object') continue;
    const prec = p as Record<string, unknown>;
    const name = typeof prec['name'] === 'string' ? prec['name'] : 'phase';
    const durMs = typeof prec['dur_ms'] === 'number' ? prec['dur_ms'] : 0;
    const statusRaw = typeof prec['status'] === 'string' ? prec['status'] : 'info';
    const tone: PhaseSegment['tone'] =
      statusRaw === 'ok' || statusRaw === 'warn' || statusRaw === 'err' ? statusRaw : 'info';
    out.push({ label: name, ms: durMs, tone });
  }
  return out;
}
