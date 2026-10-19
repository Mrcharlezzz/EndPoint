import type { ReactElement } from 'react';
import { Card, CardBody, CardHeader, Chip, KV, Kpi, PlatformChip } from '@/components/ui';
import { useSyncs, useUserStats } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { UserDetail } from '@/api/queries';
import { platformsOf } from './lib';

interface OverviewTabProps {
  user: UserDetail;
}

export function OverviewTab({ user }: OverviewTabProps): ReactElement {
  const stats = useUserStats(user.email);
  const syncs = useSyncs({ user_email: user.email, limit: 10 });
  const plats = platformsOf(user);

  const totalItems = stats.data?.total_items ?? 0;
  const errors7d = stats.data?.errors_last_7d ?? 0;
  const cursors = stats.data?.cursors ?? {};

  return (
    <div className="ud-overview">
      <div className="ud-kpi-row">
        <Kpi label="total items" value={totalItems} />
        <Kpi
          label="errors · 7d"
          value={<span style={{ color: errors7d > 0 ? 'var(--err)' : undefined }}>{errors7d}</span>}
        />
        <Kpi label="platforms" value={plats.length} />
        <Kpi
          label="last refreshed"
          value={
            <span className="mono tiny">
              {plats
                .map((p) => p.last_refreshed_at)
                .filter((v): v is string => v !== null)
                .sort()
                .slice(-1)[0] !== undefined
                ? formatRelative(
                    plats
                      .map((p) => p.last_refreshed_at)
                      .filter((v): v is string => v !== null)
                      .sort()
                      .slice(-1)[0]!,
                  )
                : '—'}
            </span>
          }
        />
      </div>

      <div className="grid-2 ud-dual">
        <Card>
          <CardHeader>Recent sync runs</CardHeader>
          <CardBody>
            {syncs.isLoading && <span className="tiny dim">loading…</span>}
            {!syncs.isLoading && (syncs.data ?? []).length === 0 && (
              <span className="tiny dim">No syncs yet for this user.</span>
            )}
            <ul className="ud-sync-list">
              {(syncs.data ?? []).slice(0, 10).map((row) => (
                <li key={row.id} className="ud-sync-row">
                  <PlatformChip name={row.platform} />
                  <Chip tone={row.state === 'failed' ? 'err' : row.state === 'running' ? 'info' : 'ok'} dot>
                    {row.state}
                  </Chip>
                  <span className="mono tiny muted ud-sync-when">
                    {row.started_at !== null ? formatRelative(row.started_at) : '—'}
                  </span>
                  <span className="mono tiny ud-sync-id" title={row.id}>
                    {row.id.slice(0, 8)}
                  </span>
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>

        <Card>
          <CardHeader>Current cursors</CardHeader>
          <CardBody>
            {Object.keys(cursors).length === 0 && (
              <span className="tiny dim">No cursors persisted yet.</span>
            )}
            {Object.entries(cursors).map(([platform, scopes]) => (
              <div key={platform} className="ud-cursor-block">
                <div className="row gap-2 ud-cursor-head">
                  <PlatformChip name={platform} />
                </div>
                <KV
                  mono
                  rows={Object.entries(scopes ?? {}).map(([scope, value]) => [
                    scope,
                    truncateJson(value),
                  ])}
                />
              </div>
            ))}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

function truncateJson(value: unknown): string {
  if (value === null || value === undefined) return '—';
  const json = JSON.stringify(value);
  return json.length > 80 ? json.slice(0, 77) + '…' : json;
}
