import { useMemo, useState, type ReactElement } from 'react';
import { FilterPill, Icon, SearchInput } from '@/components/ui';
import { LiveChip } from '@/components/LiveChip';
import { useSyncs } from '@/api/queries';
import { SyncsTable } from './SyncsTable';
import { SyncDrawer } from './SyncDrawer';
import { countByState } from './lib';
import '@/styles/pages/syncs.css';

type Filter = 'all' | 'running' | 'completed' | 'cancelled' | 'failed' | 'slow';

export function SyncsPage(): ReactElement {
  const syncs = useSyncs({ limit: 500 });
  const rows = useMemo(() => syncs.data ?? [], [syncs.data]);

  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const counts = useMemo(() => countByState(rows), [rows]);
  const running = counts['running'] ?? 0;
  const completed = counts['completed'] ?? 0;
  const failed = counts['failed'] ?? 0;
  const cancelled = counts['cancelled'] ?? 0;

  const slowCount = useMemo(() => {
    let n = 0;
    for (const r of rows) {
      if (r.state !== 'completed') continue;
      if (r.started_at === null || r.finished_at === null) continue;
      const ms = new Date(r.finished_at).getTime() - new Date(r.started_at).getTime();
      if (ms > 5000) n += 1;
    }
    return n;
  }, [rows]);

  const filteredRows = useMemo(() => {
    if (filter === 'all') return rows;
    if (filter === 'slow') {
      return rows.filter((r) => {
        if (r.state !== 'completed') return false;
        if (r.started_at === null || r.finished_at === null) return false;
        const ms = new Date(r.finished_at).getTime() - new Date(r.started_at).getTime();
        return ms > 5000;
      });
    }
    return rows.filter((r) => r.state === filter);
  }, [rows, filter]);

  // Pressing the same pill again clears the filter.
  const toggle = (next: Filter) => setFilter(filter === next ? 'all' : next);

  return (
    <>
      <header className="page-header">
        <h1>Syncs</h1>
        <span className="crumb">· all runs across all users</span>
        <span className="spacer" />
        <span className="meta">
          <LiveChip />
          <span className="sep">·</span>
          <span>
            {filteredRows.length} of {rows.length}
          </span>
        </span>
      </header>

      <div className="syncs-split">
        <div className="syncs-left">
          <div className="syncs-filterbar">
            <SearchInput
              placeholder="Search by user, sync id, platform, or error code"
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              wrapperClassName="syncs-search"
            />
            <div className="filter-row">
              <FilterPill active={filter === 'all'} count={rows.length} onClick={() => setFilter('all')}>
                All
              </FilterPill>
              <FilterPill
                active={filter === 'running'}
                count={running}
                countTone="ok"
                onClick={() => toggle('running')}
              >
                Running
              </FilterPill>
              <FilterPill
                active={filter === 'completed'}
                count={completed}
                countTone="ok"
                onClick={() => toggle('completed')}
              >
                Completed
              </FilterPill>
              <FilterPill
                active={filter === 'cancelled'}
                count={cancelled}
                countTone="warn"
                onClick={() => toggle('cancelled')}
              >
                Cancelled
              </FilterPill>
              <FilterPill
                active={filter === 'failed'}
                count={failed}
                countTone="err"
                onClick={() => toggle('failed')}
              >
                Failed
              </FilterPill>
              <FilterPill
                active={filter === 'slow'}
                count={slowCount}
                countTone="warn"
                onClick={() => toggle('slow')}
              >
                Slow
              </FilterPill>
              <span className="sep" />
              <button type="button" className="pill" aria-haspopup="true">
                Platform
                <Icon name="chevron-down" size={10} />
              </button>
              <button type="button" className="pill" aria-haspopup="true">
                Last 24h
                <Icon name="chevron-down" size={10} />
              </button>
            </div>
          </div>

          <div className="syncs-tablewrap">
            <SyncsTable
              rows={filteredRows}
              query={query}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          </div>
        </div>

        <SyncDrawer id={selectedId} onClose={() => setSelectedId(null)} />
      </div>
    </>
  );
}
