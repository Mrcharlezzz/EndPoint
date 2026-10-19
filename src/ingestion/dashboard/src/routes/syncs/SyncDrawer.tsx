import type { ReactElement } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Drawer,
  DrawerBody,
  DrawerHeader,
  Icon,
  PhaseTrack,
  PlatformChip,
  type PhaseSegment,
} from '@/components/ui';
import { useSync } from '@/api/queries';
import {
  durationMs,
  formatClockShort,
  formatSeconds,
  itemsFetched,
  stateLabel,
  stateTone,
} from './lib';

interface SyncDrawerProps {
  id: string | null;
  onClose?: () => void;
}

export function SyncDrawer({ id, onClose }: SyncDrawerProps): ReactElement {
  if (id === null) {
    return (
      <Drawer className="syncs-drawer">
        <div className="drawer-h">
          <div className="title-row">
            <span className="id dim">Select a sync</span>
          </div>
        </div>
        <DrawerBody>
          <p className="tiny dim">Click any row to see its phase timeline, metadata, items, and raw profile.</p>
        </DrawerBody>
      </Drawer>
    );
  }
  return <SyncDrawerInner id={id} onClose={onClose} />;
}

function SyncDrawerInner({
  id,
  onClose,
}: {
  id: string;
  onClose?: (() => void) | undefined;
}): ReactElement {
  const q = useSync(id);
  const history = q.data?.history;
  const profile = q.data?.profile;

  const state = history?.state ?? 'unknown';
  const isErr = state === 'failed' || history?.error != null;
  const items = history !== undefined ? itemsFetched(history.stats) : 0;
  const dur =
    history?.started_at != null && history.finished_at != null
      ? formatSeconds(durationMs(history.started_at, history.finished_at))
      : state === 'running'
        ? 'live'
        : '—';

  const segments = buildPhaseSegments(profile);

  return (
    <Drawer className="syncs-drawer">
      <DrawerHeader
        title={
          <>
            {onClose !== undefined && (
              <Button
                variant="ghost"
                size="sm"
                onClick={onClose}
                aria-label="Close drawer"
                title="Close"
              >
                <Icon name="close" size={12} />
              </Button>
            )}
            <span className="mono" style={{ marginLeft: 6 }}>
              {id}
            </span>
            <span style={{ marginLeft: 8 }}>
              <Chip tone={stateTone(state)}>{stateLabel(state)}</Chip>
            </span>
          </>
        }
        actions={
          <>
            <Button size="sm" variant="ghost" aria-label="Copy id">
              <Icon name="copy" size={11} />
            </Button>
            <Button size="sm" variant="ghost" aria-label="Open in new tab">
              <Icon name="external" size={11} />
            </Button>
          </>
        }
        subtitle={
          history !== undefined ? (
            <>
              <span>{history.user_email}</span>
              <span className="sep">·</span>
              <PlatformChip name={history.platform} />
              <span className="fill" />
              <span>started {formatClockShort(history.started_at)} · {dur}</span>
            </>
          ) : (
            <span>loading…</span>
          )
        }
      />
      <DrawerBody>
        <Card>
          <CardHeader title="Phase timeline" right={<span className="sub">total {dur}</span>} />
          <CardBody>
            {segments.length > 0 ? (
              <PhaseTrack segments={segments} />
            ) : (
              <span className="tiny dim">No phase profile for this run.</span>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Metadata" />
          <CardBody style={{ padding: '4px 14px' }}>
            <KvRow k="sync id" v={id} mono />
            <KvRow k="state" v={state} />
            <KvRow
              k="started"
              v={history?.started_at !== null && history !== undefined ? history.started_at : '—'}
              mono
            />
            <KvRow
              k="finished"
              v={history?.finished_at ?? '—'}
              mono
            />
            <KvRow k="duration" v={dur} />
            <KvRow k="cursor before" v={formatCursor(history?.cursor_before)} mono />
            <KvRow k="cursor after" v={formatCursor(history?.cursor_after)} mono />
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Items by content_type"
            right={<span className="sub">{isErr ? '0 committed' : `${items} committed`}</span>}
          />
          <CardBody>
            {isErr ? (
              <span className="tiny dim">No items written — sync failed before first commit.</span>
            ) : (
              <ItemsByType stats={history?.stats} />
            )}
          </CardBody>
        </Card>

        {isErr && (
          <Card>
            <CardHeader title="Error" right={<span className="sub">1</span>} />
            <CardBody>
              <div className="ev err">
                <div className="ev-head">
                  <Chip tone="err">{history?.error ?? 'failed'}</Chip>
                  <span className="fill" />
                  <span className="mono dim tiny">recoverable=—</span>
                </div>
                {history?.error !== null && history !== undefined && (
                  <div className="ev-body">{history.error}</div>
                )}
                <div className="ev-act">
                  <Button size="sm">
                    <Icon name="refresh" size={11} />
                    force refresh
                  </Button>
                  <Button size="sm" variant="danger">
                    <Icon name="x-circle" size={11} />
                    mark expired
                  </Button>
                </div>
              </div>
            </CardBody>
          </Card>
        )}

        <Card>
          <CardHeader
            title="Raw sync profile"
            right={
              <Button size="sm" variant="ghost">
                <Icon name="copy" size={11} />
                copy JSON
              </Button>
            }
          />
          <CardBody flush>
            <pre className="code-block" style={{ borderRadius: 0, borderWidth: '1px 0 0 0' }}>
              {JSON.stringify(q.data ?? {}, null, 2)}
            </pre>
          </CardBody>
        </Card>
      </DrawerBody>
    </Drawer>
  );
}

function formatCursor(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return '—';
  }
}

function KvRow({ k, v, mono }: { k: string; v: string; mono?: boolean }): ReactElement {
  return (
    <div className="kv-row">
      <span className="k">{k}</span>
      <span
        className="v"
        style={{ fontFamily: mono === true ? 'var(--font-mono)' : undefined }}
      >
        {v}
      </span>
    </div>
  );
}

function ItemsByType({ stats }: { stats: unknown }): ReactElement {
  if (stats === null || stats === undefined || typeof stats !== 'object') {
    return <span className="tiny dim">No per-type breakdown available.</span>;
  }
  const rec = stats as Record<string, unknown>;
  const byType = rec['by_content_type'];
  if (byType === null || byType === undefined || typeof byType !== 'object') {
    return <span className="tiny dim">No per-type breakdown available.</span>;
  }
  const entries = Object.entries(byType as Record<string, unknown>).filter(
    (e): e is [string, number] => typeof e[1] === 'number',
  );
  if (entries.length === 0) {
    return <span className="tiny dim">No per-type breakdown available.</span>;
  }
  const total = entries.reduce((s, [, n]) => s + n, 0) || 1;
  return (
    <div className="col gap-2">
      {entries.map(([name, n]) => (
        <div className="row gap-3" key={name} style={{ fontSize: 'var(--t-sm)' }}>
          <span
            style={{
              width: 90,
              color: 'var(--text-3)',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
            }}
          >
            {name}
          </span>
          <div className="fill">
            <div className="bar ok">
              <span style={{ width: `${Math.min(100, (n / total) * 100)}%` }} />
            </div>
          </div>
          <span className="mono" style={{ minWidth: 30, textAlign: 'right' }}>
            {n}
          </span>
        </div>
      ))}
    </div>
  );
}

function buildPhaseSegments(profile: unknown): PhaseSegment[] {
  if (profile === null || profile === undefined || typeof profile !== 'object') return [];
  const rec = profile as Record<string, unknown>;
  const phases = rec['phases'];
  if (!Array.isArray(phases)) return [];
  const out: PhaseSegment[] = [];
  for (const p of phases) {
    if (p === null || typeof p !== 'object') continue;
    const prec = p as Record<string, unknown>;
    const name = typeof prec['name'] === 'string' ? prec['name'] : 'phase';
    const durMs = typeof prec['dur_ms'] === 'number' ? prec['dur_ms'] : 0;
    const statusRaw = typeof prec['status'] === 'string' ? prec['status'] : 'info';
    const tone: PhaseSegment['tone'] =
      statusRaw === 'ok' || statusRaw === 'warn' || statusRaw === 'err' ? statusRaw : 'info';
    out.push({ label: name, ms: durMs, tone });
  }
  return out;
}
