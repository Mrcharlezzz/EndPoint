import { useEffect, useMemo, useState, type ReactElement } from 'react';
import { Chip, PlatformChip } from '@/components/ui';
import { cn } from '@/lib/cn';
import { useChannel } from '@/ws/useChannel';
import type { SyncHistoryItem } from '@/api/queries';
import {
  durationMs,
  formatClockShort,
  formatSeconds,
  itemsFetched,
  matchesQuery,
  stateLabel,
  stateTone,
} from './lib';

interface SyncsTableProps {
  rows: readonly SyncHistoryItem[];
  /** Free-text query applied after tab/pill filtering. */
  query: string;
  /** Subset of states to include; undefined = all. */
  stateFilter?: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

interface RunningProgress {
  pct: number;
  phase: string;
}

/**
 * Render running syncs pinned at the top, followed by a divider, then the
 * filtered history. Clicking any row raises `onSelect(id)` so the parent
 * can swap in the drawer contents.
 */
export function SyncsTable({
  rows,
  query,
  stateFilter,
  selectedId,
  onSelect,
}: SyncsTableProps): ReactElement {
  const [progress, setProgress] = useState<Record<string, RunningProgress>>({});
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  useChannel('/ws/syncs', (e) => {
    const id = typeof e.sync_id === 'string' ? e.sync_id : undefined;
    if (id === undefined) return;
    const eventName = typeof e.event === 'string' ? e.event : '';
    if (eventName === 'sync.started') {
      setProgress((p) => ({ ...p, [id]: { pct: 5, phase: 'starting' } }));
    } else if (eventName === 'sync.progress') {
      const pct = typeof e['progress'] === 'number' ? e['progress'] : undefined;
      const phase = typeof e['phase'] === 'string' ? e['phase'] : undefined;
      if (pct !== undefined || phase !== undefined) {
        setProgress((p) => ({
          ...p,
          [id]: {
            pct: pct ?? p[id]?.pct ?? 5,
            phase: phase ?? p[id]?.phase ?? 'running',
          },
        }));
      }
    } else if (eventName === 'sync.finished' || eventName === 'sync.failed') {
      setProgress((p) => {
        if (!(id in p)) return p;
        const next = { ...p };
        delete next[id];
        return next;
      });
    }
  });

  const { running, history } = useMemo(() => {
    const running: SyncHistoryItem[] = [];
    const history: SyncHistoryItem[] = [];
    for (const r of rows) {
      if (r.state === 'running') running.push(r);
      else history.push(r);
    }
    return { running, history };
  }, [rows]);

  const filteredHistory = useMemo(() => {
    return history.filter((r) => {
      if (stateFilter !== undefined && r.state !== stateFilter) return false;
      return matchesQuery(r, query);
    });
  }, [history, stateFilter, query]);

  const filteredRunning = useMemo(() => {
    if (stateFilter !== undefined && stateFilter !== 'running') return [];
    return running.filter((r) => matchesQuery(r, query));
  }, [running, stateFilter, query]);

  return (
    <table className="t">
      <thead>
        <tr>
          <th style={{ width: 18 }} />
          <th style={{ width: 84 }}>started</th>
          <th style={{ width: 170 }}>user</th>
          <th style={{ width: 70 }}>plat</th>
          <th>scope</th>
          <th style={{ width: 200 }}>progress / phase</th>
          <th className="right" style={{ width: 60 }}>
            dur
          </th>
          <th className="right" style={{ width: 70 }}>
            items
          </th>
          <th className="right" style={{ width: 50 }}>
            errs
          </th>
          <th style={{ width: 70 }}>state</th>
        </tr>
      </thead>
      <tbody>
        {filteredRunning.map((r) => (
          <RunningRow
            key={r.id}
            r={r}
            progress={progress[r.id]}
            selected={selectedId === r.id}
            onClick={() => onSelect(r.id)}
            nowMs={nowMs}
          />
        ))}
        {filteredRunning.length > 0 && filteredHistory.length > 0 && (
          <tr className="divider-row">
            <td colSpan={10} />
          </tr>
        )}
        {filteredRunning.length === 0 && filteredHistory.length === 0 && (
          <tr>
            <td colSpan={10} className="dim tiny" style={{ padding: '16px 20px' }}>
              No syncs match the current filters.
            </td>
          </tr>
        )}
        {filteredHistory.map((r) => {
          const ms = durationMs(r.started_at, r.finished_at);
          const items = itemsFetched(r.stats);
          const isErr = r.state === 'failed' || r.error !== null;
          const phaseSummary =
            isErr && r.error !== null
              ? `failed · ${r.error}`
              : r.state === 'cancelled'
                ? 'cancelled'
                : 'complete';
          return (
            <tr
              key={r.id}
              className={cn(selectedId === r.id && 'active')}
              onClick={() => onSelect(r.id)}
              style={{ cursor: 'pointer' }}
            >
              <td />
              <td className="mono">{formatClockShort(r.started_at)}</td>
              <td className="mono pri">{r.user_email.split('@')[0]}</td>
              <td>
                <PlatformChip name={r.platform} />
              </td>
              <td className="mono">—</td>
              <td className="mono tiny muted" style={{ fontSize: 10 }}>
                {phaseSummary}
              </td>
              <td className="num">{ms > 0 ? formatSeconds(ms) : '—'}</td>
              <td className="num">{items}</td>
              <td className={cn('num', isErr ? 'text-err' : 'dim')}>{isErr ? 1 : '—'}</td>
              <td>
                <Chip tone={stateTone(r.state)}>{stateLabel(r.state)}</Chip>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

interface RunningRowProps {
  r: SyncHistoryItem;
  progress: RunningProgress | undefined;
  selected: boolean;
  onClick: () => void;
  nowMs: number;
}

function RunningRow({ r, progress, selected, onClick, nowMs }: RunningRowProps): ReactElement {
  const pct = Math.max(0, Math.min(100, progress?.pct ?? 5));
  const phase = progress?.phase ?? 'running';
  const elapsed =
    r.started_at !== null
      ? Math.floor((nowMs - new Date(r.started_at).getTime()) / 1000)
      : 0;
  return (
    <tr
      className={cn('running', selected && 'active')}
      onClick={onClick}
      style={{ cursor: 'pointer' }}
    >
      <td>
        <span className="dotpulse" />
      </td>
      <td className="mono dim">running</td>
      <td className="mono pri">{r.user_email.split('@')[0]}</td>
      <td>
        <PlatformChip name={r.platform} />
      </td>
      <td className="mono">—</td>
      <td>
        <div className="col gap-1">
          <div className="row gap-2">
            <div className="bar run fill">
              <span style={{ width: `${pct}%` }} />
            </div>
            <span className="mono" style={{ fontSize: 10, minWidth: 30, textAlign: 'right' }}>
              {Math.round(pct)}%
            </span>
          </div>
          <span className="mono tiny muted" style={{ fontSize: 10 }}>
            {phase}
          </span>
        </div>
      </td>
      <td className="num mono">{elapsed}s</td>
      <td className="num dim">—</td>
      <td className="num dim">—</td>
      <td>
        <Chip tone="run">run</Chip>
      </td>
    </tr>
  );
}
