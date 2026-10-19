import { useEffect, useState, type ReactElement } from 'react';
import { Link } from 'react-router-dom';
import { Button, Chip, ConfirmModal, Icon } from '@/components/ui';
import {
  useAdaptersHealth,
  useCredentials,
  useEmergencyStop,
  useSetDrainMode,
  useConfig,
  useUsers,
} from '@/api/queries';
import { useChannel } from '@/ws/useChannel';
import { cn } from '@/lib/cn';

function AdaptersCount(): ReactElement {
  const q = useAdaptersHealth();
  const data = q.data;
  if (data === undefined) {
    return <span className="mono dim">adapters —</span>;
  }
  const unhealthy = data.total_enabled - data.total_healthy;
  const tone = unhealthy === 0 ? 'dim' : 'err';
  // Tooltip lists per-platform detail so operators can see which is off
  // without drilling elsewhere.
  const tooltip = data.platforms
    .map((p) => {
      if (!p.enabled) return `${p.platform}: disabled`;
      if (p.healthy === null) return `${p.platform}: ${p.error ?? 'not probed'}`;
      if (p.healthy) return `${p.platform}: healthy`;
      return `${p.platform}: ${p.error ?? 'unhealthy'}`;
    })
    .join('\n');
  return (
    <span className={`mono ${tone}`} title={tooltip}>
      {data.total_healthy}/{data.total_enabled} adapters healthy
    </span>
  );
}

export function StatusStrip(): ReactElement {
  const config = useConfig();
  const draining = config.data?.scheduler?.drain_mode ?? false;
  const emergencyRequested = config.data?.scheduler?.emergency_stop_requested ?? false;

  const setDrain = useSetDrainMode();
  const emergencyStop = useEmergencyStop();

  const [openDrain, setOpenDrain] = useState(false);
  const [openStop, setOpenStop] = useState(false);

  const credentials = useCredentials();
  const users = useUsers();
  // Expiring-within-24h is a rolling window so we tick the clock here rather
  // than reading Date.now() during render. 60s is fine — the window moves in
  // hours.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);
  const expired = credentials.data?.filter((c) => c.is_expired).length ?? 0;
  const expiringSoon =
    credentials.data?.filter((c) => {
      if (c.is_expired) return false;
      if (c.expired_at === null) return false;
      const delta = new Date(c.expired_at).getTime() - nowMs;
      return delta > 0 && delta < 24 * 60 * 60 * 1000;
    }).length ?? 0;
  const pausedUsers = users.data?.filter((u) => u.any_paused).length ?? 0;

  // Live snapshot from the metrics WS channel. Falls back to em-dash
  // while we wait for the first tick so we never render stale zeros.
  const [snapshot, setSnapshot] = useState<{
    in_flight?: number;
    force_sync_pending?: number;
    dead_letter_suppressed?: number;
  }>({});
  useChannel('/ws/metrics', (event) => {
    const next: {
      in_flight?: number;
      force_sync_pending?: number;
      dead_letter_suppressed?: number;
    } = {};
    if (typeof event['in_flight'] === 'number') next.in_flight = event['in_flight'];
    if (typeof event['force_sync_pending'] === 'number') {
      next.force_sync_pending = event['force_sync_pending'];
    }
    if (typeof event['dead_letter_suppressed'] === 'number') {
      next.dead_letter_suppressed = event['dead_letter_suppressed'];
    }
    setSnapshot(next);
  });

  const fmt = (n: number | undefined) => (n === undefined ? '—' : n.toString());
  // "Severe" = anything the operator should *notice immediately*. Red
  // left-edge + subtle pulse makes bad days visually unmissable.
  const severe = emergencyRequested || expired > 0;
  const statusTone = emergencyRequested ? 'err' : draining ? 'warn' : 'ok';
  const statusLabel = emergencyRequested
    ? 'emergency stop'
    : draining
      ? 'draining'
      : 'system running';
  const statusIcon = emergencyRequested ? 'stop' : draining ? 'pause' : 'check';

  return (
    <div className={cn('card status-strip', severe && 'severe')}>
      <div
        className="card-b"
        style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 14 }}
      >
        {severe && (
          <span className="status-alert-glyph" aria-hidden="true">
            <Icon name="alert" size={14} />
          </span>
        )}
        <div className="row gap-3">
          <Chip tone={statusTone} className="status-chip">
            <Icon name={statusIcon} size={10} />
            {statusLabel}
          </Chip>
          <AdaptersCount />
        </div>
        <span style={{ width: 1, height: 20, background: 'var(--divider)' }} />
        <span className="mono dim" style={{ fontSize: 11 }}>
          workers {fmt(snapshot.in_flight)} · pending {fmt(snapshot.force_sync_pending)} ·
          suppressed {fmt(snapshot.dead_letter_suppressed)}
          {expired > 0 && (
            <>
              {' · '}
              <Link to="/errors?filter=expired_credential" className="text-err attn-link">
                {expired} expired
              </Link>
            </>
          )}
          {expiringSoon > 0 && (
            <>
              {' · '}
              <span className="text-warn">{expiringSoon} expiring</span>
            </>
          )}
          {pausedUsers > 0 && (
            <>
              {' · '}
              <Link to="/users?filter=paused" className="text-warn attn-link">
                {pausedUsers} paused
              </Link>
            </>
          )}
        </span>
        <span className="fill" />
        <Button size="sm" variant="ghost" onClick={() => setOpenDrain(true)}>
          <Icon name="pause" size={11} />
          {draining ? 'exit drain' : 'drain'}
        </Button>
        <Button size="sm" variant="danger" onClick={() => setOpenStop(true)}>
          <Icon name="stop" size={11} />
          emergency stop
        </Button>
      </div>

      <ConfirmModal
        open={openDrain}
        title={draining ? 'Exit drain mode?' : 'Enter drain mode?'}
        body={
          <p className="tiny dim">
            {draining
              ? 'Scheduler resumes starting new syncs.'
              : 'Scheduler finishes running syncs but will not start new ones.'}
          </p>
        }
        confirmLabel={draining ? 'Exit drain' : 'Enter drain'}
        requireReason
        busy={setDrain.isPending}
        onCancel={() => setOpenDrain(false)}
        onConfirm={async (reason) => {
          await setDrain.mutateAsync({ enabled: !draining, reason: reason ?? '' });
          setOpenDrain(false);
        }}
      />

      <ConfirmModal
        open={openStop}
        title="Emergency stop?"
        body={
          <p className="tiny dim">
            Cancels in-flight syncs and raises the kill switch. All pages will reflect the stop once
            the scheduler drains.
          </p>
        }
        confirmLabel="Emergency stop"
        variant="danger"
        typedConfirm="STOP"
        requireReason
        busy={emergencyStop.isPending}
        onCancel={() => setOpenStop(false)}
        onConfirm={async (reason) => {
          await emergencyStop.mutateAsync({ confirm: 'STOP', reason: reason ?? '' });
          setOpenStop(false);
        }}
      />
    </div>
  );
}
