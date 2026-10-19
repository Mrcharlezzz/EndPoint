import { useEffect, useMemo, useState, type ReactElement } from 'react';
import { Outlet, NavLink } from 'react-router-dom';
import { Icon, type IconName } from '@/components/ui';
import { useAbout } from '@/api/queries';
import { useChannel } from '@/ws/useChannel';
import type { ChannelStatus } from '@/ws/types';
import { cn } from '@/lib/cn';

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  end?: boolean;
  count?: number;
}

interface NavGroup {
  title: string;
  items: NavItem[];
}

const NAV: NavGroup[] = [
  {
    title: 'observe',
    items: [
      { to: '/', label: 'Overview', icon: 'dashboard', end: true },
      { to: '/users', label: 'Users', icon: 'users' },
      { to: '/syncs', label: 'Syncs', icon: 'sync' },
      { to: '/errors', label: 'Errors', icon: 'alert' },
    ],
  },
  {
    title: 'manage',
    items: [
      { to: '/credentials', label: 'Credentials', icon: 'key' },
      { to: '/logs', label: 'Logs', icon: 'log' },
      { to: '/alerts', label: 'Alerts', icon: 'bell' },
      { to: '/audit', label: 'Audit', icon: 'audit' },
    ],
  },
  {
    title: 'system',
    items: [
      { to: '/config', label: 'Config', icon: 'config' },
      { to: '/settings', label: 'Settings', icon: 'settings' },
    ],
  },
];

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  if (h < 24) return rem > 0 ? `${h}h ${rem}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH > 0 ? `${d}d ${remH}h` : `${d}d`;
}

function EnvVersionChip(): ReactElement {
  const about = useAbout();
  const env = about.data?.environment ?? '—';
  const version = about.data?.daemon_version ?? '—';
  return <span className="env">{env} · v{version}</span>;
}

function UptimeChip(): ReactElement {
  const about = useAbout();
  // Capture a ticking clock in state so we never call Date.now() during
  // render. 1s cadence keeps the "Xs" and "Xm" boundaries responsive;
  // the daemon's wall-clock start time is fixed for the session.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const label = useMemo(() => {
    const started = about.data?.started_at;
    if (started === undefined) return '—';
    const ms = nowMs - new Date(started).getTime();
    if (Number.isNaN(ms) || ms < 0) return '—';
    return formatUptime(ms / 1000);
  }, [about.data?.started_at, nowMs]);
  return (
    <span className="mono muted" style={{ fontSize: 11 }}>
      uptime {label}
    </span>
  );
}

function ConnectionChip(): ReactElement {
  const { status } = useChannel('/ws/metrics', () => {
    /* we only care about connection status here; metrics values land
       via dedicated subscribers on pages that need them */
  });
  const label: Record<ChannelStatus, string> = {
    connecting: 'connecting',
    connected: 'connected',
    reconnecting: 'reconnecting',
    disconnected: 'offline',
  };
  return (
    <span className={cn('conn-chip', status)} title={`WebSocket: ${label[status]}`}>
      <span className="dot" />
      <span>{label[status]}</span>
    </span>
  );
}

export function ShellLayout(): ReactElement {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          <span>ingestor</span>
        </div>
        <EnvVersionChip />
        <UptimeChip />
        <span className="spacer" />
        <button
          className="palette"
          onClick={(e) => e.preventDefault()}
          aria-label="Jump to user, sync, error"
          type="button"
        >
          <Icon name="search" size={12} />
          <span>Jump to user, sync, error code…</span>
          <span className="kbd">⌘K</span>
        </button>
        <ConnectionChip />
      </header>
      <aside className="sidebar">
        {NAV.map((group) => (
          <div className="nav-group" key={group.title}>
            <div className="nav-heading">{group.title}</div>
            {group.items.map((item) => {
              const end = item.end ?? false;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={end}
                  className={({ isActive }) => cn('nav-item', isActive && 'active')}
                >
                  <span className="icon">
                    <Icon name={item.icon} size={13} />
                  </span>
                  <span>{item.label}</span>
                  {item.count !== undefined && <span className="count">{item.count}</span>}
                </NavLink>
              );
            })}
          </div>
        ))}
        <div className="foot">
          <div>prod</div>
          <div>pg pool 32</div>
          <div>ws 1</div>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
