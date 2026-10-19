import type { ReactElement } from 'react';
import { Link } from 'react-router-dom';
import { Button, Chip, Icon } from '@/components/ui';
import { useForceSyncAll, usePauseUser, useUnpauseUser } from '@/api/queries';
import type { UserDetail } from '@/api/queries';
import { detailHealth, healthLabel, healthTone, initialsOf, platformsOf } from './lib';

interface HeaderProps {
  user: UserDetail;
}

export function Header({ user }: HeaderProps): ReactElement {
  const plats = platformsOf(user);
  const anyPaused = plats.some((p) => p.is_paused);
  const pause = usePauseUser();
  const unpause = useUnpauseUser();
  const forceAll = useForceSyncAll();
  const h = detailHealth(user);

  return (
    <header className="user-detail-header">
      <div className="ud-h-main">
        <Link to="/users" className="btn ghost sm ud-back">
          <Icon name="arrow-right" size={11} />
          <span className="ud-back-label">back</span>
        </Link>
        <span className="avatar lg">{initialsOf(user)}</span>
        <div className="col ud-ident">
          <span className="mono ud-email">{user.email}</span>
          <span className="mono muted tiny">{user.display_name ?? '—'}</span>
        </div>
        <Chip tone={healthTone(h)} dot>
          {healthLabel(h)}
        </Chip>
        <span className="spacer" />
        <div className="row gap-2">
          {anyPaused ? (
            <Button
              size="sm"
              onClick={() => void unpause.mutateAsync({ email: user.email })}
              disabled={unpause.isPending}
            >
              <Icon name="refresh" size={11} />
              resume
            </Button>
          ) : (
            <Button
              size="sm"
              onClick={() => void pause.mutateAsync({ email: user.email })}
              disabled={pause.isPending}
            >
              <Icon name="pause" size={11} />
              pause
            </Button>
          )}
          <Button
            size="sm"
            variant="primary"
            onClick={() => void forceAll.mutateAsync({ email: user.email })}
            disabled={forceAll.isPending || plats.length === 0}
          >
            <Icon name="refresh" size={11} />
            force sync all
          </Button>
        </div>
      </div>
    </header>
  );
}
