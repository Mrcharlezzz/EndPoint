import type { ReactElement } from 'react';
import { Chip, PlatformChip } from '@/components/ui';
import { cn } from '@/lib/cn';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { UserListItem } from '@/api/queries';
import {
  extractPlatforms,
  healthLabel,
  healthTone,
  userHealth,
  type PlatformState,
} from './lib';

interface UsersTableProps {
  rows: readonly UserListItem[];
  selectedEmail: string | null;
  checkedEmails: ReadonlySet<string>;
  onSelectRow: (email: string) => void;
  onToggleCheck: (email: string) => void;
  onToggleAll: () => void;
}

export function UsersTable({
  rows,
  selectedEmail,
  checkedEmails,
  onSelectRow,
  onToggleCheck,
  onToggleAll,
}: UsersTableProps): ReactElement {
  const allChecked = rows.length > 0 && rows.every((u) => checkedEmails.has(u.email));
  const someChecked = rows.some((u) => checkedEmails.has(u.email)) && !allChecked;

  return (
    <table className="t">
      <thead>
        <tr>
          <th style={{ width: 32 }}>
            <input
              type="checkbox"
              aria-label="Select all"
              checked={allChecked}
              ref={(el) => {
                if (el !== null) el.indeterminate = someChecked;
              }}
              onChange={onToggleAll}
              onClick={(e) => e.stopPropagation()}
            />
          </th>
          <th style={{ width: 16 }} />
          <th>user</th>
          <th style={{ width: 140 }}>platforms</th>
          <th style={{ width: 100 }}>last sync</th>
          <th className="right" style={{ width: 80 }}>
            items 24h
          </th>
          <th style={{ width: 90 }}>state</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={7} className="dim tiny" style={{ padding: '16px 20px' }}>
              No users match the current filters.
            </td>
          </tr>
        )}
        {rows.map((u) => {
          const h = userHealth(u);
          const plats = extractPlatforms(u);
          const lastSync = bestLastSync(plats);
          const items24h = totalItems24h(plats);
          const isChecked = checkedEmails.has(u.email);
          return (
            <tr
              key={u.email}
              className={cn(selectedEmail === u.email && 'active')}
              onClick={() => onSelectRow(u.email)}
              style={{ cursor: 'pointer' }}
            >
              <td onClick={(e) => e.stopPropagation()}>
                <input
                  type="checkbox"
                  aria-label={`Select ${u.email}`}
                  checked={isChecked}
                  onChange={() => onToggleCheck(u.email)}
                />
              </td>
              <td>
                {h === 'err' && <span className="status-rail err" />}
                {h === 'warn' && <span className="status-rail warn" />}
              </td>
              <td>
                <div className="col" style={{ gap: 0 }}>
                  <span className="mono pri" style={{ fontSize: 12 }}>
                    {u.email.split('@')[0]}
                  </span>
                  <span className="mono muted" style={{ fontSize: 10 }}>
                    {u.display_name ?? u.email}
                  </span>
                </div>
              </td>
              <td>
                <div className="row gap-1" style={{ flexWrap: 'wrap' }}>
                  {plats.length === 0 && <span className="tiny muted">none</span>}
                  {plats.map((p) => (
                    <PlatformChip key={p.platform} name={p.platform} />
                  ))}
                </div>
              </td>
              <td className="mono dim">{lastSync !== null ? formatRelative(lastSync) : '—'}</td>
              <td className="num">{items24h ?? '—'}</td>
              <td>
                <Chip tone={healthTone(h)}>{healthLabel(h)}</Chip>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function bestLastSync(plats: readonly PlatformState[]): string | null {
  let best: number | null = null;
  for (const p of plats) {
    if (p.last_successful_sync_at === null) continue;
    const t = new Date(p.last_successful_sync_at).getTime();
    if (Number.isNaN(t)) continue;
    if (best === null || t > best) best = t;
  }
  return best === null ? null : new Date(best).toISOString();
}

function totalItems24h(plats: readonly PlatformState[]): number | null {
  // The UserListItem shape doesn't currently surface items-today per platform.
  // Wire this up when the backend exposes it; return null so the column shows —.
  void plats;
  return null;
}
