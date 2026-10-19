import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { useChannel } from '@/ws/useChannel';
import { listChannels, subscribeChannelSnapshots, type ChannelSnapshot } from '@/ws/manager';
import { cn } from '@/lib/cn';

/**
 * Page-header pulse + debug popover reflecting the daemon WebSocket state.
 *
 * Subscribes to ``/ws/metrics`` purely to track its connection
 * status — the payload is ignored; a dedicated subscriber on each
 * page handles the metrics values themselves.
 *
 * - connected    → "LIVE" (green pulsing dot)
 * - connecting / reconnecting → muted grey pulse
 * - disconnected → "OFFLINE" red, no pulse
 *
 * Clicking the chip opens a popover listing every WS channel the app is
 * currently subscribed to — useful when the page "looks stuck" and the
 * operator wants to know which channel is dead without the DevTools pane.
 */
export function LiveChip(): ReactElement {
  const { status } = useChannel('/ws/metrics', () => {
    /* status-only: page-level subscribers handle metrics payloads */
  });
  const label =
    status === 'connected'
      ? 'LIVE'
      : status === 'disconnected'
        ? 'OFFLINE'
        : status === 'reconnecting'
          ? 'RECONNECTING'
          : 'CONNECTING';

  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent): void => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <span ref={wrapRef} className="live-wrap">
      <button
        type="button"
        className={cn('live live-trigger', status !== 'connected' && status)}
        title={`WebSocket: ${status} — click for details`}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
      >
        {label}
      </button>
      {open && <LivePopover />}
    </span>
  );
}

function LivePopover(): ReactElement {
  // Snapshots are refreshed via the manager subscription — not
  // useSyncExternalStore, because listChannels() returns a fresh array on
  // each call and React's identity check would warn about unstable snapshots.
  const [snapshots, setSnapshots] = useState<ChannelSnapshot[]>(() => listChannels());
  useEffect(() => {
    const unsub = subscribeChannelSnapshots(() => setSnapshots(listChannels()));
    return unsub;
  }, []);
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const sorted = useMemo(
    () => [...snapshots].sort((a, b) => a.path.localeCompare(b.path)),
    [snapshots],
  );

  return (
    <div className="live-popover" role="dialog" aria-label="WebSocket channel status">
      <div className="live-popover-head">
        <span className="tiny dim">WebSocket channels</span>
        <span className="tiny dim">{sorted.length} active</span>
      </div>
      {sorted.length === 0 && <div className="tiny dim live-popover-empty">No subscriptions.</div>}
      {sorted.map((s) => (
        <ChannelRow key={`${s.path}::${s.filterKey}`} snap={s} nowMs={nowMs} />
      ))}
    </div>
  );
}

function ChannelRow({ snap, nowMs }: { snap: ChannelSnapshot; nowMs: number }): ReactElement {
  const tone = snap.status === 'connected' ? 'ok' : snap.status === 'disconnected' ? 'err' : 'warn';
  return (
    <div className="live-popover-row">
      <span className={`live-dot live-dot-${tone}`} aria-hidden="true" />
      <span className="mono tiny live-popover-path">{snap.path}</span>
      <span className="tiny dim live-popover-meta">
        {snap.status}
        {snap.retries > 0 && ` · retry ${snap.retries}`}
      </span>
      <span className="tiny dim live-popover-last">{formatLastMsg(snap, nowMs)}</span>
    </div>
  );
}

function formatLastMsg(snap: ChannelSnapshot, nowMs: number): string {
  if (snap.lastMessageAt === null) return snap.messageCount === 0 ? 'no msgs yet' : '—';
  const delta = Math.max(0, Math.floor((nowMs - snap.lastMessageAt) / 1000));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  return `${Math.floor(delta / 3600)}h ago`;
}
