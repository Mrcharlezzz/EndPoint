import { useMemo, type ReactElement } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Icon,
  KV,
  PlatformChip,
} from '@/components/ui';
import {
  useCredentials,
  useForceRefreshTeams,
  useForceSync,
  useMarkExpired,
} from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { CredentialSummary, UserDetail } from '@/api/queries';

interface CredentialsTabProps {
  user: UserDetail;
}

export function CredentialsTab({ user }: CredentialsTabProps): ReactElement {
  const creds = useCredentials();
  const rows = useMemo(
    () => (creds.data ?? []).filter((c) => c.user_email === user.email),
    [creds.data, user.email],
  );

  if (creds.isLoading) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">loading credentials…</span>
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">No credentials connected for this user.</span>
      </div>
    );
  }

  return (
    <div className="ud-creds">
      {rows.map((c) => (
        <CredentialCard key={c.platform} cred={c} />
      ))}
    </div>
  );
}

function CredentialCard({ cred }: { cred: CredentialSummary }): ReactElement {
  const markExpired = useMarkExpired();
  const forceSync = useForceSync();
  const refreshTeams = useForceRefreshTeams();
  const isTeams = cred.platform === 'teams';

  const tone = cred.is_expired ? 'err' : cred.is_paused ? 'idle' : 'ok';
  const label = cred.is_expired ? 'expired' : cred.is_paused ? 'paused' : 'connected';

  return (
    <Card className="ud-cred-card">
      <CardHeader>
        <div className="row gap-2">
          <PlatformChip name={cred.platform} />
          <span className="spacer" />
          <Chip tone={tone} dot>
            {label}
          </Chip>
        </div>
      </CardHeader>
      <CardBody>
        <KV
          mono
          rows={[
            ['platform', cred.platform],
            ['user', cred.user_email],
            [
              'last refreshed',
              cred.last_refreshed_at !== null ? formatRelative(cred.last_refreshed_at) : '—',
            ],
            ['expired at', cred.expired_at !== null ? formatRelative(cred.expired_at) : '—'],
            ['refresh url', cred.refresh_url === '' ? '—' : cred.refresh_url],
          ]}
        />
        <div className="row gap-2 ud-cred-actions">
          {isTeams && (
            <Button
              size="sm"
              variant="primary"
              onClick={() => void refreshTeams.mutateAsync({ email: cred.user_email })}
              disabled={refreshTeams.isPending}
            >
              <Icon name="refresh" size={11} />
              force refresh
            </Button>
          )}
          <Button
            size="sm"
            onClick={() =>
              void forceSync.mutateAsync({ email: cred.user_email, platform: cred.platform })
            }
            disabled={forceSync.isPending || cred.is_expired}
          >
            <Icon name="sync" size={11} />
            force sync
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              void markExpired.mutateAsync({
                email: cred.user_email,
                platform: cred.platform,
              })
            }
            disabled={markExpired.isPending || cred.is_expired}
          >
            <Icon name="x-circle" size={11} />
            mark expired
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
