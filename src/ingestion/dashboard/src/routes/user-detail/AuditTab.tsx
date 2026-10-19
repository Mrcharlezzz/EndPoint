import { useMemo, type ReactElement } from 'react';
import { Chip } from '@/components/ui';
import { useAudit } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { AuditEntry, UserDetail } from '@/api/queries';

interface AuditTabProps {
  user: UserDetail;
}

export function AuditTab({ user }: AuditTabProps): ReactElement {
  const q = useAudit({ target: user.email, limit: 200 });
  // Server-side `target` is a prefix match against many shapes (`email`,
  // `email/platform`). Sort newest-first for display.
  const rows = useMemo(() => {
    const list = q.data ?? [];
    return [...list].sort((a, b) => {
      const ta = typeof a.timestamp === 'string' ? Date.parse(a.timestamp) : 0;
      const tb = typeof b.timestamp === 'string' ? Date.parse(b.timestamp) : 0;
      return tb - ta;
    });
  }, [q.data]);

  if (q.isLoading) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">loading audit…</span>
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="ud-pad">
        <span className="tiny dim">No audit entries affecting this user.</span>
      </div>
    );
  }
  return (
    <ul className="ud-audit-list">
      {rows.map((row, idx) => (
        <AuditItem key={`${row.timestamp ?? idx}-${idx}`} row={row} />
      ))}
    </ul>
  );
}

function AuditItem({ row }: { row: AuditEntry }): ReactElement {
  const r = row as unknown as Record<string, unknown>;
  const action = typeof r['action'] === 'string' ? (r['action']) : '—';
  const actor = typeof r['actor'] === 'string' ? (r['actor']) : '—';
  const target = typeof r['target'] === 'string' ? (r['target']) : '—';
  const reason = typeof r['reason'] === 'string' ? (r['reason']) : null;
  const ts = typeof r['timestamp'] === 'string' ? (r['timestamp']) : null;
  return (
    <li className="ud-audit-row">
      <div className="row gap-2">
        <Chip tone="info">{action}</Chip>
        <span className="mono tiny">{target}</span>
        <span className="spacer" />
        <span className="mono tiny muted">{actor}</span>
        <span className="mono tiny muted">{ts !== null ? formatRelative(ts) : '—'}</span>
      </div>
      {reason !== null && <div className="tiny ud-audit-reason">{reason}</div>}
    </li>
  );
}
