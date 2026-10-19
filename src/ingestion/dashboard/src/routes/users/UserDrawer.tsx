import type { ReactElement } from 'react';
import { Link } from 'react-router-dom';
import {
  Button,
  Chip,
  Drawer,
  DrawerBody,
  DrawerHeader,
  DrawerSectionHeading,
  Icon,
  KV,
  Kpi,
  PlatformChip,
} from '@/components/ui';
import { useUser, useForceRefreshTeams, usePauseUser, useUnpauseUser } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import {
  extractPlatforms,
  healthLabel,
  healthTone,
  initialsFor,
  userHealth,
  type PlatformState,
} from './lib';
import type { UserListItem } from '@/api/queries';

interface UserDrawerProps {
  user: UserListItem | null;
  onClose?: () => void;
}

export function UserDrawer({ user, onClose }: UserDrawerProps): ReactElement {
  if (user === null) {
    return (
      <Drawer className="users-drawer">
        <div className="drawer-h">
          <div className="title-row">
            <span className="id dim">Select a user</span>
          </div>
        </div>
        <DrawerBody>
          <p className="tiny dim">
            Click any row to see per-platform health, recent activity, and quick actions.
          </p>
        </DrawerBody>
      </Drawer>
    );
  }
  return <UserDrawerInner user={user} onClose={onClose} />;
}

function UserDrawerInner({
  user,
  onClose,
}: {
  user: UserListItem;
  onClose?: (() => void) | undefined;
}): ReactElement {
  const detail = useUser(user.email);
  const pause = usePauseUser();
  const unpause = useUnpauseUser();
  const refresh = useForceRefreshTeams();

  const h = userHealth(user);
  const plats = extractPlatforms(user);
  const hasTeams = plats.some((p) => p.platform === 'teams');

  return (
    <Drawer className="users-drawer">
      <DrawerHeader
        title={
          <>
            {onClose !== undefined && (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Close drawer"
                onClick={onClose}
              >
                <Icon name="close" size={12} />
              </Button>
            )}
            <span className="avatar lg" style={{ marginLeft: 6 }}>
              {initialsFor(user)}
            </span>
            <div className="col" style={{ gap: 0, marginLeft: 8, minWidth: 0 }}>
              <span className="mono" style={{ fontSize: 13 }}>
                {user.email.split('@')[0]}
              </span>
              <span className="mono muted" style={{ fontSize: 11 }}>
                {user.display_name ?? user.email}
              </span>
            </div>
            <span style={{ marginLeft: 'auto' }}>
              <Chip tone={healthTone(h)} dot>
                {healthLabel(h)}
              </Chip>
            </span>
          </>
        }
      />
      <DrawerBody>
        <div className="row gap-2">
          {user.any_paused ? (
            <Button size="sm" onClick={() => void unpause.mutateAsync({ email: user.email })}>
              <Icon name="refresh" size={11} />
              resume
            </Button>
          ) : (
            <Button size="sm" onClick={() => void pause.mutateAsync({ email: user.email })}>
              <Icon name="pause" size={11} />
              pause
            </Button>
          )}
          {hasTeams && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => void refresh.mutateAsync({ email: user.email })}
            >
              <Icon name="refresh" size={11} />
              force refresh Teams
            </Button>
          )}
          <Link to={`/users/${encodeURIComponent(user.email)}`} className="btn ghost sm">
            open profile
            <Icon name="arrow-right" size={11} />
          </Link>
        </div>

        <section>
          <DrawerSectionHeading>Platforms</DrawerSectionHeading>
          <div className="col gap-2">
            {plats.length === 0 && <span className="tiny dim">No platforms connected.</span>}
            {plats.map((p) => (
              <PlatformRow key={p.platform} p={p} />
            ))}
          </div>
        </section>

        <section>
          <DrawerSectionHeading>Activity · last 24h</DrawerSectionHeading>
          <div className="grid-2" style={{ gap: 8 }}>
            <Kpi label="platforms" size="small" value={plats.length} />
            <Kpi
              label="expired"
              size="small"
              value={
                <span style={{ color: user.any_expired ? 'var(--err)' : undefined }}>
                  {plats.filter((p) => p.expired).length}
                </span>
              }
            />
          </div>
        </section>

        <section>
          <DrawerSectionHeading>Metadata</DrawerSectionHeading>
          <KV
            mono
            rows={[
              ['email', user.email],
              ['display name', user.display_name ?? '—'],
              ['paused', user.any_paused ? 'yes' : 'no'],
              ['expired', user.any_expired ? 'yes' : 'no'],
              ['platforms', plats.length === 0 ? '—' : plats.map((p) => p.platform).join(', ')],
              ['detail', detail.isLoading ? 'loading…' : detail.isError ? 'unavailable' : 'ready'],
            ]}
          />
        </section>
      </DrawerBody>
    </Drawer>
  );
}

function PlatformRow({ p }: { p: PlatformState }): ReactElement {
  const toneLabel = p.expired ? 'expired' : p.connected ? 'connected' : 'never';
  const tone = p.expired ? 'err' : p.connected ? 'ok' : 'idle';
  return (
    <div className="plat-row">
      <div className="row gap-2">
        <PlatformChip name={p.platform} />
        <span className="fill" />
        <Chip tone={tone} dot>
          {toneLabel}
        </Chip>
      </div>
      <div className="mono tiny muted" style={{ marginTop: 4 }}>
        last sync {p.last_successful_sync_at !== null ? formatRelative(p.last_successful_sync_at) : '—'}
        {p.last_refreshed_at !== null && (
          <>
            {' · refreshed '}
            {formatRelative(p.last_refreshed_at)}
          </>
        )}
      </div>
    </div>
  );
}
