import { useState, type ReactElement } from 'react';
import { Chip, PlatformChip } from '@/components/ui';
import { useSyncs } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { SyncHistoryItem, UserDetail } from '@/api/queries';
import { SyncDrawer } from './SyncDrawer';

interface SyncHistoryTabProps {
  user: UserDetail;
}

export function SyncHistoryTab({ user }: SyncHistoryTabProps): ReactElement {
  const [selected, setSelected] = useState<string | null>(null);
  const syncs = useSyncs({ user_email: user.email, limit: 100 });
  const rows = syncs.data ?? [];

  return (
    <div className="ud-syncs">
      <div className="ud-syncs-tablewrap">
        <table className="ud-table">
          <thead>
            <tr>
              <th>started</th>
              <th>platform</th>
              <th>scope</th>
              <th>duration</th>
              <th>items</th>
              <th>errors</th>
              <th>state</th>
            </tr>
          </thead>
          <tbody>
            {syncs.isLoading && (
              <tr>
                <td colSpan={7} className="tiny dim">
                  loading…
                </td>
              </tr>
            )}
            {!syncs.isLoading && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="tiny dim">
                  No sync history for this user.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <SyncRow
                key={row.id}
                row={row}
                selected={selected === row.id}
                onClick={() => setSelected(row.id)}
              />
            ))}
          </tbody>
        </table>
      </div>
      <SyncDrawer syncId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function SyncRow({
  row,
  selected,
  onClick,
}: {
  row: SyncHistoryItem;
  selected: boolean;
  onClick: () => void;
}): ReactElement {
  const stats = (row.stats ?? {}) as Record<string, unknown>;
  const items = typeof stats['items_fetched'] === 'number' ? stats['items_fetched'] : '—';
  const errs = typeof stats['errors'] === 'number' ? stats['errors'] : 0;
  const duration =
    row.started_at !== null && row.finished_at !== null
      ? `${Math.round(
          (new Date(row.finished_at).getTime() - new Date(row.started_at).getTime()) / 1000,
        )}s`
      : '—';
  const scope = typeof stats['scope'] === 'string' ? stats['scope'] : '—';
  const tone: 'ok' | 'err' | 'run' | 'idle' =
    row.state === 'failed' ? 'err' : row.state === 'running' ? 'run' : row.state === 'complete' || row.state === 'completed' ? 'ok' : 'idle';
  return (
    <tr
      onClick={onClick}
      className={selected ? 'selected' : ''}
      style={{ cursor: 'pointer' }}
    >
      <td className="mono tiny">
        {row.started_at !== null ? formatRelative(row.started_at) : '—'}
      </td>
      <td>
        <PlatformChip name={row.platform} />
      </td>
      <td className="mono tiny">{scope}</td>
      <td className="mono tiny">{duration}</td>
      <td className="mono tiny">{items}</td>
      <td className="mono tiny">{errs}</td>
      <td>
        <Chip tone={tone} dot>
          {row.state}
        </Chip>
      </td>
    </tr>
  );
}
