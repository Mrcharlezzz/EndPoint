import { Fragment, useMemo, useState, type ReactElement } from 'react';
import { Card, CardBody, CardHeader, Chip, Icon, PlatformChip } from '@/components/ui';
import { LiveChip } from '@/components/LiveChip';
import { useChannel } from '@/ws/useChannel';
import { displayUserLabel, formatClock } from './lib';
import { cn } from '@/lib/cn';
import type { ErrorsResponse } from '@/api/queries';

// Covers both shapes: WS /ws/errors (severity/code/message/timestamp) and
// REST /api/errors (kind/error/occurred_at). Every field is optional; the
// ``extract`` helper normalises them into a canonical ErrorFields shape.
type ErrorRow = Record<string, unknown>;

type Severity = 'info' | 'warn' | 'err' | 'critical';
type Tone = 'info' | 'warn' | 'err';

interface ErrorFields {
  sev: Severity;
  tone: Tone;
  code: string;
  message: string | null;
  timestamp: string | undefined;
  userEmail: string | null;
  platform: string | null;
}

interface Group {
  key: string;
  fields: ErrorFields[];
  sev: Severity;
  tone: Tone;
  code: string;
  platform: string | null;
  exampleMessage: string | null;
  latestTimestamp: string | undefined;
  uniqueUsers: number;
  exampleUser: string | null;
}

interface ErrorsFeedProps {
  initial: ErrorsResponse | undefined;
  limit?: number;
}

export function ErrorsFeed({ initial, limit = 50 }: ErrorsFeedProps): ReactElement {
  const [wsRows, setWsRows] = useState<ErrorRow[]>([]);

  useChannel('/ws/errors', (e) => {
    setWsRows((prev) => [e as ErrorRow, ...prev].slice(0, limit));
  });

  const rows = useMemo<ErrorRow[]>(() => {
    const items = initial?.items;
    const base: ErrorRow[] = Array.isArray(items) ? (items as ErrorRow[]) : [];
    return [...wsRows, ...base].slice(0, limit);
  }, [initial, wsRows, limit]);

  const groups = useMemo<Group[]>(() => buildGroups(rows), [rows]);

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (key: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const totalRows = rows.length;
  // Prune stale keys so `expanded.size` doesn't accumulate as group keys
  // disappear from the window — and so hiddenByCollapse reflects only
  // currently-displayed groups.
  const effectiveExpanded = useMemo(() => {
    const alive = new Set(groups.map((g) => g.key));
    return new Set([...expanded].filter((k) => alive.has(k)));
  }, [expanded, groups]);
  const hiddenByCollapse =
    totalRows -
    groups.length -
    [...effectiveExpanded].reduce((acc, k) => {
      const g = groups.find((x) => x.key === k);
      return acc + (g === undefined ? 0 : Math.max(0, g.fields.length - 1));
    }, 0);

  return (
    <Card className="feed-card">
      <CardHeader
        title={<LiveChip />}
        right={
          <>
            <span className="sub">
              {groups.length} group{groups.length === 1 ? '' : 's'} · {totalRows} events
            </span>
            {hiddenByCollapse > 0 && <span className="tiny dim">· {hiddenByCollapse} hidden</span>}
          </>
        }
      >
        <span className="title">Recent errors</span>
      </CardHeader>
      <CardBody flush className="feed-body">
        <table className="t">
          <thead>
            <tr>
              <th style={{ width: 88 }}>time</th>
              <th style={{ width: 52 }}>sev</th>
              <th style={{ width: 130 }}>user</th>
              <th style={{ width: 80 }}>plat</th>
              <th style={{ width: 140 }}>code</th>
              <th>message</th>
              <th style={{ width: 28 }} />
            </tr>
          </thead>
          <tbody>
            {groups.length === 0 && (
              <tr>
                <td colSpan={7} className="dim tiny" style={{ padding: '12px 16px' }}>
                  No errors in the window.
                </td>
              </tr>
            )}
            {groups.map((g) => {
              const isExpanded = effectiveExpanded.has(g.key);
              const count = g.fields.length;
              const clickable = count > 1;
              const userCell =
                g.uniqueUsers > 1
                  ? `${g.uniqueUsers} users`
                  : g.exampleUser !== null
                    ? displayUserLabel(g.exampleUser)
                    : '—';
              return (
                <Fragment key={g.key}>
                  <tr
                    className={cn('sev-row', `sev-${g.tone}`, clickable && 'group-head')}
                    onClick={clickable ? () => toggle(g.key) : undefined}
                    style={clickable ? { cursor: 'pointer' } : undefined}
                  >
                    <td className="mono">{formatClock(g.latestTimestamp)}</td>
                    <td>
                      <Chip tone={g.tone}>{g.sev}</Chip>
                    </td>
                    <td
                      className="mono"
                      title={
                        g.uniqueUsers === 1 && g.exampleUser !== null ? g.exampleUser : undefined
                      }
                    >
                      {userCell}
                    </td>
                    <td>{g.platform !== null ? <PlatformChip name={g.platform} /> : '—'}</td>
                    <td
                      className="mono"
                      style={{ color: g.tone === 'err' ? 'var(--err)' : 'var(--warn)' }}
                    >
                      {g.code}
                    </td>
                    <td className="dim group-message">
                      <MessageCell message={g.exampleMessage} />
                      {count > 1 && <span className="group-count">× {count}</span>}
                    </td>
                    <td className="group-toggle">
                      {clickable && (
                        <button
                          type="button"
                          className={cn('group-chevron', isExpanded && 'open')}
                          aria-expanded={isExpanded}
                          aria-label={`${isExpanded ? 'Collapse' : 'Expand'} ${count} ${g.code} events`}
                          onClick={(e) => {
                            // Row has its own click handler for mouse users;
                            // stop propagation so this isn't double-toggled.
                            e.stopPropagation();
                            toggle(g.key);
                          }}
                        >
                          <Icon name="chevron-down" size={12} />
                        </button>
                      )}
                    </td>
                  </tr>
                  {isExpanded &&
                    g.fields.map((f, i) => (
                      <tr key={`${g.key}::${i}`} className={`sev-row sev-${g.tone} group-child`}>
                        <td className="mono">{formatClock(f.timestamp)}</td>
                        <td />
                        <td className="mono" title={f.userEmail ?? undefined}>
                          {displayUserLabel(f.userEmail)}
                        </td>
                        <td />
                        <td />
                        <td className="dim">
                          <MessageCell message={f.message} />
                        </td>
                        <td />
                      </tr>
                    ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

function MessageCell({ message }: { message: string | null }): ReactElement {
  const text = message ?? '—';
  const copyable = message !== null && message.length > 0;
  return (
    <span className="msg-cell">
      <span className="msg-text">{text}</span>
      {copyable && (
        <button
          type="button"
          className="msg-copy"
          aria-label="Copy error message"
          onClick={(e) => {
            e.stopPropagation();
            void navigator.clipboard?.writeText(text);
          }}
        >
          <Icon name="copy" size={11} />
        </button>
      )}
    </span>
  );
}

function buildGroups(rows: readonly ErrorRow[]): Group[] {
  const map = new Map<string, Group>();
  for (const r of rows) {
    const f = extract(r);
    const key = `${f.sev}::${f.code}::${f.platform ?? '_'}`;
    let g = map.get(key);
    if (g === undefined) {
      g = {
        key,
        fields: [],
        sev: f.sev,
        tone: f.tone,
        code: f.code,
        platform: f.platform,
        exampleMessage: f.message,
        latestTimestamp: f.timestamp,
        uniqueUsers: 0,
        exampleUser: f.userEmail,
      };
      map.set(key, g);
    }
    g.fields.push(f);
  }
  for (const g of map.values()) {
    const users = new Set<string>();
    for (const f of g.fields) {
      if (f.userEmail !== null && f.userEmail.length > 0) users.add(f.userEmail);
    }
    g.uniqueUsers = users.size;
    if (g.exampleUser === null && users.size > 0) {
      g.exampleUser = users.values().next().value ?? null;
    }
  }
  return Array.from(map.values()).sort((a, b) => {
    const at = a.latestTimestamp ?? '';
    const bt = b.latestTimestamp ?? '';
    return bt.localeCompare(at);
  });
}

function extract(r: ErrorRow): ErrorFields {
  const sev = resolveSev(r);
  const tone: Tone = sev === 'critical' ? 'err' : sev;
  // WS events use short field names; REST rows use ``kind`` for the error
  // identity. Fall through both.
  const code = strOrUndef(r['code']) ?? strOrUndef(r['kind']) ?? '—';
  const message = strOrUndef(r['message']) ?? strOrUndef(r['error']) ?? null;
  const timestamp = strOrUndef(r['timestamp']) ?? strOrUndef(r['occurred_at']);
  const userEmail = strOrUndef(r['user_email']) ?? null;
  const platform = strOrUndef(r['platform']) ?? null;
  return { sev, tone, code, message, timestamp, userEmail, platform };
}

function strOrUndef(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}

function resolveSev(r: ErrorRow): Severity {
  const raw = r['severity'];
  if (raw === 'info' || raw === 'warn' || raw === 'critical') return raw;
  if (raw === 'err' || raw === 'error') return 'err';
  // REST rows use ``kind`` to imply severity: expired_credential is a warning,
  // everything else (failed_sync / dead_letter) is an error.
  const kind = r['kind'];
  if (kind === 'expired_credential') return 'warn';
  return 'err';
}
