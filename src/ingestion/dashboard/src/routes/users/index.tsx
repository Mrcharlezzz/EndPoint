import { useCallback, useMemo, useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';
import { LiveChip } from '@/components/LiveChip';
import { FilterPill, Icon, SearchInput } from '@/components/ui';
import { useUsers } from '@/api/queries';
import { UsersTable } from './UsersTable';
import { UserDrawer } from './UserDrawer';
import { UsersBulkBar } from './UsersBulkBar';
import { matchesUserQuery, userHealth } from './lib';
import '@/styles/pages/users.css';

type Filter = 'all' | 'healthy' | 'warning' | 'failing' | 'paused';
const VALID_FILTERS: readonly Filter[] = ['all', 'healthy', 'warning', 'failing', 'paused'];

function filterFromSearch(params: URLSearchParams): Filter {
  const raw = params.get('filter');
  return (VALID_FILTERS as readonly string[]).includes(raw ?? '') ? (raw as Filter) : 'all';
}

export function UsersPage(): ReactElement {
  const users = useUsers();
  const rows = useMemo(() => users.data ?? [], [users.data]);

  // Filter lives in the URL so it's shareable and so inbound links from
  // other pages (e.g. StatusStrip ``/users?filter=paused``) drive the UI
  // even when we're already mounted on this route.
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
  const [selectedEmail, setSelectedEmail] = useState<string | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<string>>(() => new Set());

  const counts = useMemo(() => {
    const c = { healthy: 0, warning: 0, failing: 0, paused: 0 };
    for (const u of rows) {
      const h = userHealth(u);
      if (h === 'ok') c.healthy += 1;
      else if (h === 'warn') c.warning += 1;
      else if (h === 'err') c.failing += 1;
      else c.paused += 1;
    }
    return c;
  }, [rows]);

  const filtered = useMemo(() => {
    return rows.filter((u) => {
      if (!matchesUserQuery(u, query)) return false;
      if (filter === 'all') return true;
      const h = userHealth(u);
      if (filter === 'healthy') return h === 'ok';
      if (filter === 'warning') return h === 'warn';
      if (filter === 'failing') return h === 'err';
      return h === 'paused';
    });
  }, [rows, filter, query]);

  const selectedUser = useMemo(
    () => (selectedEmail === null ? null : (rows.find((u) => u.email === selectedEmail) ?? null)),
    [rows, selectedEmail],
  );

  const toggle = (next: Filter) => setFilter(filter === next ? 'all' : next);

  function toggleCheck(email: string): void {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email);
      else next.add(email);
      return next;
    });
  }

  function toggleAll(): void {
    setChecked((prev) => {
      const allOn = filtered.every((u) => prev.has(u.email));
      if (allOn) {
        const next = new Set(prev);
        for (const u of filtered) next.delete(u.email);
        return next;
      }
      const next = new Set(prev);
      for (const u of filtered) next.add(u.email);
      return next;
    });
  }

  const checkedList = useMemo(() => Array.from(checked), [checked]);

  return (
    <>
      <header className="page-header">
        <h1>Users</h1>
        <span className="crumb">
          · {rows.length} total · {counts.paused} paused
        </span>
        <span className="spacer" />
        <span className="meta">
          <LiveChip />
          <span className="sep">·</span>
          <span>
            {counts.healthy} healthy · {counts.warning} warning · {counts.failing} failing
          </span>
        </span>
      </header>

      <div className="users-split">
        <div className="users-left">
          <div className="users-filterbar">
            <SearchInput
              placeholder="Search by name, email"
              value={query}
              onChange={(e) => setQuery(e.currentTarget.value)}
              wrapperClassName="users-search"
            />
            <div className="filter-row">
              <FilterPill
                active={filter === 'all'}
                count={rows.length}
                onClick={() => setFilter('all')}
              >
                All
              </FilterPill>
              <FilterPill
                active={filter === 'healthy'}
                count={counts.healthy}
                countTone="ok"
                onClick={() => toggle('healthy')}
              >
                Healthy
              </FilterPill>
              <FilterPill
                active={filter === 'warning'}
                count={counts.warning}
                countTone="warn"
                onClick={() => toggle('warning')}
              >
                Warning
              </FilterPill>
              <FilterPill
                active={filter === 'failing'}
                count={counts.failing}
                countTone="err"
                onClick={() => toggle('failing')}
              >
                Failing
              </FilterPill>
              <FilterPill
                active={filter === 'paused'}
                count={counts.paused}
                onClick={() => toggle('paused')}
              >
                Paused
              </FilterPill>
              <span className="sep" />
              <button type="button" className="pill" aria-haspopup="true">
                Platform
                <Icon name="chevron-down" size={10} />
              </button>
            </div>
          </div>

          <UsersBulkBar selectedEmails={checkedList} onClear={() => setChecked(new Set())} />

          <div className="users-tablewrap">
            <UsersTable
              rows={filtered}
              selectedEmail={selectedEmail}
              checkedEmails={checked}
              onSelectRow={setSelectedEmail}
              onToggleCheck={toggleCheck}
              onToggleAll={toggleAll}
            />
          </div>
        </div>

        <UserDrawer user={selectedUser} onClose={() => setSelectedEmail(null)} />
      </div>
    </>
  );
}
