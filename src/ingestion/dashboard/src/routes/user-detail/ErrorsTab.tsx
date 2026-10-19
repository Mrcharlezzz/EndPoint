import type { ReactElement } from 'react';
import { Chip, PlatformChip } from '@/components/ui';
import { useErrors } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { UserDetail } from '@/api/queries';

interface ErrorsTabProps {
  user: UserDetail;
}

export function ErrorsTab({ user }: ErrorsTabProps): ReactElement {
  const q = useErrors({ user_email: user.email, limit: 100 });
  const entries = q.data?.items ?? [];

  if (q.isLoading) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">loading errors…</span>
      </div>
    );
  }
  if (entries.length === 0) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">No errors recorded for this user.</span>
      </div>
    );
  }
  return (
    <ul className="ud-err-list">
      {entries.map((raw, idx) => {
        const e = raw as Record<string, unknown>;
        const severity = typeof e['severity'] === 'string' ? (e['severity']) : 'error';
        const tone: 'err' | 'warn' | 'info' =
          severity === 'critical' || severity === 'error' ? 'err' : severity === 'warn' ? 'warn' : 'info';
        const code = typeof e['code'] === 'string' ? (e['code']) : '—';
        const message = typeof e['message'] === 'string' ? (e['message']) : '';
        const platform = typeof e['platform'] === 'string' ? (e['platform']) : null;
        const ts = typeof e['timestamp'] === 'string' ? (e['timestamp']) : null;
        return (
          <li key={idx} className="ud-err-row">
            <Chip tone={tone} dot>
              {severity}
            </Chip>
            {platform !== null && <PlatformChip name={platform} />}
            <span className="mono tiny">{code}</span>
            <span className="tiny ud-err-msg">{message}</span>
            <span className="mono tiny muted ud-err-when">
              {ts !== null ? formatRelative(ts) : '—'}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
