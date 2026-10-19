import { useCallback, useMemo, useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Chip, FilterPill, Icon, PlatformChip, SearchInput } from '@/components/ui';
import { LiveChip } from '@/components/LiveChip';
import { useAbout, useErrors } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { ErrorItem } from '@/api/queries';
import { ErrorsDrawer } from './ErrorsDrawer';
import { countByKind, glitchtipIssuesUrl, kindLabel, matchesSearch } from './lib';
import '@/styles/pages/errors.css';

type Filter = 'all' | 'dead_letter' | 'failed_sync' | 'expired_credential';
const VALID_FILTERS: readonly Filter[] = [
  'all',
  'dead_letter',
  'failed_sync',
  'expired_credential',
];

function filterFromSearch(params: URLSearchParams): Filter {
  const raw = params.get('filter');
  return (VALID_FILTERS as readonly string[]).includes(raw ?? '') ? (raw as Filter) : 'all';
}

export function ErrorsPage(): ReactElement {
  const errors = useErrors({ limit: 200 });
  const about = useAbout();
  const rows = useMemo(() => errors.data?.items ?? [], [errors.data]);

  // Filter lives in the URL so it's shareable and so inbound links from
  // other pages (e.g. StatusStrip ``/errors?filter=expired_credential``)
  // drive the UI even when we're already mounted on this route.
  const [searchParams, setSearchParams] = useSearchParams();
  const filter = filterFromSearch(searchParams);
  const setFilter = useCallback(
    (next: Filter) => {
      setSearchParams(
        (prev) => {
          const p = new URLSearchParams(prev);
          if (next === 'all') p.delete('filter');
          else p.set('filter', next);
          return p;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const [query, setQuery] = useState('');
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);

  const counts = useMemo(() => countByKind(rows), [rows]);

  const filtered = useMemo(() => {
    return rows.filter((row) => {
      if (!matchesSearch(row, query)) return false;
      if (filter === 'all') return true;
      return row.kind === filter;
    });
  }, [rows, filter, query]);

  const selected = selectedIdx === null ? null : (filtered[selectedIdx] ?? null);

  const aboutData = about.data;
  const glitchtipUrl = aboutData?.glitchtip_url ?? '';
  const glitchtipOrg = aboutData?.glitchtip_org_slug ?? '';
  const glitchtipIssues = glitchtipIssuesUrl(glitchtipUrl, glitchtipOrg);

  const toggle = (next: Filter) => setFilter(filter === next ? 'all' : next);

  return (
    <>
      <header className="page-header">
        <h1>Errors</h1>
        <span className="crumb">· operator action queue</span>
        <span className="spacer" />
        <span className="meta">
          <LiveChip />
          <span className="sep">·</span>
          <span>
            {filtered.length} of {rows.length}
          </span>
          {glitchtipIssues !== '' && (
            <>
              <span className="sep">·</span>
              <a
                href={glitchtipIssues}
                target="_blank"
                rel="noreferrer noopener"
                className="btn sm errors-gt-link"
              >
                <Icon name="external" size={11} />
                Open GlitchTip
              </a>
            </>
          )}
        </span>
      </header>

      <div className="errors-split">
        <div className="errors-left">
          <div className="errors-filterbar">
            <SearchInput
              placeholder="Search by user, platform, sync, message…"
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              wrapperClassName="errors-search"
            />
            <div className="filter-row">
              <FilterPill
                active={filter === 'all'}
                count={counts.all}
                onClick={() => setFilter('all')}
              >
                All
              </FilterPill>
              <FilterPill
                active={filter === 'dead_letter'}
                count={counts.dead_letter}
                countTone="err"
                onClick={() => toggle('dead_letter')}
              >
                Dead letter
              </FilterPill>
              <FilterPill
                active={filter === 'failed_sync'}
                count={counts.failed_sync}
                countTone="err"
                onClick={() => toggle('failed_sync')}
              >
                Failed sync
              </FilterPill>
              <FilterPill
                active={filter === 'expired_credential'}
                count={counts.expired_credential}
                countTone="warn"
                onClick={() => toggle('expired_credential')}
              >
                Expired credential
              </FilterPill>
            </div>
          </div>

          <div className="errors-tablewrap">
            <table className="errors-table">
              <thead>
                <tr>
                  <th>when</th>
                  <th>kind</th>
                  <th>user</th>
                  <th>platform</th>
                  <th>details</th>
                </tr>
              </thead>
              <tbody>
                {errors.isLoading && (
                  <tr>
                    <td colSpan={5} className="tiny dim">
                      loading…
                    </td>
                  </tr>
                )}
                {!errors.isLoading && filtered.length === 0 && (
                  <tr>
                    <td colSpan={5} className="tiny dim">
                      No errors match the current filter.
                    </td>
                  </tr>
                )}
                {filtered.map((row, idx) => (
                  <ErrorRow
                    key={`${row.kind}-${row.user_email ?? ''}-${row.platform ?? ''}-${row.sync_id ?? idx}`}
                    row={row}
                    selected={selectedIdx === idx}
                    onClick={() => setSelectedIdx(idx)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <ErrorsDrawer
          row={selected}
          glitchtipUrl={glitchtipUrl}
          glitchtipOrgSlug={glitchtipOrg}
          onClose={() => setSelectedIdx(null)}
        />
      </div>
    </>
  );
}

function ErrorRow({
  row,
  selected,
  onClick,
}: {
  row: ErrorItem;
  selected: boolean;
  onClick: () => void;
}): ReactElement {
  const tone: 'err' | 'warn' = row.kind === 'expired_credential' ? 'warn' : 'err';
  const detail =
    row.error !== null && row.error !== undefined && row.error !== ''
      ? truncate(row.error, 120)
      : row.kind === 'dead_letter'
        ? `${row.consecutive_failures ?? 0} consecutive failures`
        : row.kind === 'expired_credential'
          ? 'Credential expired — user must re-auth'
          : '—';
  return (
    <tr onClick={onClick} className={selected ? 'selected' : ''} style={{ cursor: 'pointer' }}>
      <td className="mono tiny">
        {row.occurred_at !== null && row.occurred_at !== undefined
          ? formatRelative(row.occurred_at)
          : '—'}
      </td>
      <td>
        <Chip tone={tone} dot>
          {kindLabel(row.kind)}
        </Chip>
      </td>
      <td className="mono tiny">{row.user_email ?? '—'}</td>
      <td>
        {row.platform !== null && row.platform !== undefined ? (
          <PlatformChip name={row.platform} />
        ) : (
          <span className="tiny dim">—</span>
        )}
      </td>
      <td className="tiny errors-detail">{detail}</td>
    </tr>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
