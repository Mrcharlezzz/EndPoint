import type { ReactElement, ReactNode } from 'react';
import { Button, Chip, Drawer, DrawerBody, DrawerHeader, Icon, PlatformChip } from '@/components/ui';
import {
  useClearDeadLetter,
  useForceRefreshTeams,
  useForceSync,
  useMarkExpired,
} from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { ErrorItem } from '@/api/queries';
import { glitchtipDeepLink, kindLabel } from './lib';

interface ErrorsDrawerProps {
  row: ErrorItem | null;
  glitchtipUrl: string;
  glitchtipOrgSlug: string;
  onClose: () => void;
}

export function ErrorsDrawer({
  row,
  glitchtipUrl,
  glitchtipOrgSlug,
  onClose,
}: ErrorsDrawerProps): ReactElement {
  if (row === null) {
    return (
      <Drawer className="errors-drawer">
        <div className="drawer-h">
          <span className="id dim">Select an error</span>
        </div>
        <DrawerBody>
          <p className="tiny dim">
            Click a row to see details and per-kind operator actions.
          </p>
        </DrawerBody>
      </Drawer>
    );
  }
  return (
    <Inner
      row={row}
      glitchtipUrl={glitchtipUrl}
      glitchtipOrgSlug={glitchtipOrgSlug}
      onClose={onClose}
    />
  );
}

function Inner({
  row,
  glitchtipUrl,
  glitchtipOrgSlug,
  onClose,
}: {
  row: ErrorItem;
  glitchtipUrl: string;
  glitchtipOrgSlug: string;
  onClose: () => void;
}): ReactElement {
  const clearDL = useClearDeadLetter();
  const markExpired = useMarkExpired();
  const forceSync = useForceSync();
  const refreshTeams = useForceRefreshTeams();
  const deepLink = glitchtipDeepLink(glitchtipUrl, glitchtipOrgSlug, row);

  const email = row.user_email ?? '';
  const platform = row.platform ?? '';
  const hasEmailAndPlatform = email !== '' && platform !== '';

  return (
    <Drawer className="errors-drawer">
      <DrawerHeader
        title={
          <>
            <Button size="sm" variant="ghost" aria-label="Close drawer" onClick={onClose}>
              <Icon name="close" size={12} />
            </Button>
            <Chip tone="err" dot>
              {kindLabel(row.kind)}
            </Chip>
            {platform !== '' && <PlatformChip name={platform} />}
            {deepLink !== '' && (
              <a
                href={deepLink}
                target="_blank"
                rel="noreferrer noopener"
                className="btn sm errors-gt-deep"
              >
                <Icon name="external" size={11} />
                GlitchTip
              </a>
            )}
          </>
        }
        subtitle={
          <div className="errors-dr-grid">
            <Field label="kind" value={kindLabel(row.kind)} />
            <Field label="platform" value={platform === '' ? '—' : platform} />
            <Field
              label="occurred"
              value={
                row.occurred_at !== null && row.occurred_at !== undefined
                  ? formatRelative(row.occurred_at)
                  : '—'
              }
            />
            <Field
              label="consec. fails"
              value={
                row.consecutive_failures === null || row.consecutive_failures === undefined
                  ? '—'
                  : String(row.consecutive_failures)
              }
            />
            <Field label="user" value={email === '' ? '—' : email} fullWidth />
            <Field label="sync id" value={row.sync_id ?? '—'} fullWidth />
            <Field
              label="suppressed until"
              value={
                row.suppressed_until === null || row.suppressed_until === undefined
                  ? '—'
                  : formatRelative(row.suppressed_until)
              }
              fullWidth
            />
          </div>
        }
      />
      <DrawerBody>
        {row.error !== null && row.error !== undefined && row.error !== '' && (
          <section>
            <div className="drawer-section-h">Error</div>
            <pre className="errors-pre">{row.error}</pre>
          </section>
        )}

        {hasEmailAndPlatform && (
          <section className="errors-dr-actions">
            <div className="drawer-section-h">Actions</div>
            <div className="row gap-2 errors-actions">
              {row.kind === 'dead_letter' && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() =>
                    void clearDL.mutateAsync({ email, platform, reason: 'Cleared from dashboard' })
                  }
                  disabled={clearDL.isPending}
                >
                  <Icon name="check" size={11} />
                  clear dead letter
                </Button>
              )}
              {row.kind === 'expired_credential' && platform === 'teams' && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => void refreshTeams.mutateAsync({ email })}
                  disabled={refreshTeams.isPending}
                >
                  <Icon name="refresh" size={11} />
                  force refresh teams
                </Button>
              )}
              <Button
                size="sm"
                onClick={() =>
                  void forceSync.mutateAsync({ email, platform, reason: 'From errors page' })
                }
                disabled={forceSync.isPending}
              >
                <Icon name="sync" size={11} />
                force sync
              </Button>
              {row.kind !== 'expired_credential' && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    void markExpired.mutateAsync({
                      email,
                      platform,
                      reason: 'Marked from errors page',
                    })
                  }
                  disabled={markExpired.isPending}
                >
                  <Icon name="x-circle" size={11} />
                  mark expired
                </Button>
              )}
            </div>
          </section>
        )}
      </DrawerBody>
    </Drawer>
  );
}

function Field({
  label,
  value,
  fullWidth,
}: {
  label: string;
  value: ReactNode;
  fullWidth?: boolean;
}): ReactElement {
  return (
    <div className={`errors-dr-field${fullWidth === true ? ' full' : ''}`}>
      <span className="k">{label}</span>
      <span className="v">{value}</span>
    </div>
  );
}
