import { useState, type ReactElement } from 'react';
import { PlatformChip } from '@/components/ui';
import { useUserItems } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { UserDetail, UserItemRow } from '@/api/queries';
import { ItemDrawer } from './ItemDrawer';

interface ContributionsTabProps {
  user: UserDetail;
}

export function ContributionsTab({ user }: ContributionsTabProps): ReactElement {
  const [selected, setSelected] = useState<string | null>(null);
  const q = useUserItems(user.email, { limit: 100 });
  const rows = q.data ?? [];

  return (
    <div className="ud-contrib">
      <div className="ud-contrib-tablewrap">
        <table className="ud-table">
          <thead>
            <tr>
              <th>external_id</th>
              <th>platform</th>
              <th>type</th>
              <th>title</th>
              <th>course</th>
              <th>published</th>
              <th>ingested</th>
            </tr>
          </thead>
          <tbody>
            {q.isLoading && (
              <tr>
                <td colSpan={7} className="tiny dim">
                  loading…
                </td>
              </tr>
            )}
            {!q.isLoading && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="tiny dim">
                  No ingested items attributed to this user yet.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <ItemRow
                key={row.id}
                row={row}
                selected={selected === row.id}
                onClick={() => setSelected(row.id)}
              />
            ))}
          </tbody>
        </table>
      </div>
      <ItemDrawer itemId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function ItemRow({
  row,
  selected,
  onClick,
}: {
  row: UserItemRow;
  selected: boolean;
  onClick: () => void;
}): ReactElement {
  return (
    <tr
      onClick={onClick}
      className={selected ? 'selected' : ''}
      style={{ cursor: 'pointer' }}
    >
      <td className="mono tiny" title={row.external_id}>
        {row.external_id.length > 24 ? row.external_id.slice(0, 22) + '…' : row.external_id}
      </td>
      <td>
        <PlatformChip name={row.platform} />
      </td>
      <td className="mono tiny">{row.content_type ?? '—'}</td>
      <td className="tiny" style={{ maxWidth: 360 }}>
        {row.title ?? <span className="dim">—</span>}
      </td>
      <td className="tiny">{row.course_name ?? <span className="dim">—</span>}</td>
      <td className="mono tiny">
        {row.published_at !== null ? formatRelative(row.published_at) : '—'}
      </td>
      <td className="mono tiny">{formatRelative(row.ingested_at)}</td>
    </tr>
  );
}
