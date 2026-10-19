import { useCallback, useMemo, useRef, useState, type ReactElement } from 'react';
import {
  Card,
  CardBody,
  CardHeader,
  Chip,
  HeatCell,
  Sparkline,
  type ChipTone,
} from '@/components/ui';
import { useAdaptersHealth } from '@/api/queries';
import { bucketHeatmap, capitalize, KNOWN_PLATFORMS, type KnownPlatform } from './lib';
import { formatRelative } from '@/hooks/useRelativeTime';
import type { SyncHistoryItem, CredentialSummary } from '@/api/queries';
import { PlatformDrillDrawer } from './PlatformDrillDrawer';

interface PlatformPanelProps {
  syncs: readonly SyncHistoryItem[];
  credentials: readonly CredentialSummary[];
}

interface ProbeState {
  enabled: boolean;
  healthy: boolean | null;
  error: string | null;
}

interface RowStats {
  tone: ChipTone;
  label: string;
  tooltip: string | undefined;
  lastSync: string;
  errPct: number;
  credsLabel: string;
}

export function PlatformPanel({ syncs, credentials }: PlatformPanelProps): ReactElement {
  const health = useAdaptersHealth();
  const buckets = useMemo(() => bucketHeatmap(syncs), [syncs]);
  const throughput = useMemo(() => hourlyThroughputByPlatform(syncs), [syncs]);
  const currentHour = new Date().getHours();

  const probeByPlatform = new Map<string, ProbeState>();
  for (const p of health.data?.platforms ?? []) {
    probeByPlatform.set(p.platform, {
      enabled: p.enabled,
      healthy: p.healthy,
      error: p.error,
    });
  }

  const [selected, setSelected] = useState<KnownPlatform | null>(null);
  // Remember which row opened the drawer so focus returns to it on close —
  // WAI-ARIA dialog pattern.
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const closeDrawer = useCallback((): void => {
    setSelected(null);
    openerRef.current?.focus();
  }, []);

  return (
    <Card>
      <CardHeader title="Platform health · last 24h" right={<Legend />} />
      <CardBody className="pp-body">
        <div className="pp-hour-labels" aria-hidden="true">
          <span />
          {Array.from({ length: 24 }).map((_, i) => (
            <span key={i}>{i % 3 === 0 ? String(i).padStart(2, '0') : ''}</span>
          ))}
        </div>
        <div className="pp-rows">
          {/* Now-cursor overlay — absolutely positioned, mirrors the row grid
              template so the highlighted column aligns across all platforms. */}
          <div className="pp-now-layer" aria-hidden="true">
            <span />
            {Array.from({ length: 24 }).map((_, i) => (
              <span key={i} className={i === currentHour ? 'now' : ''} />
            ))}
          </div>
          {KNOWN_PLATFORMS.map((plat) => {
            const stats = computeRowStats(plat, syncs, probeByPlatform.get(plat), credentials);
            const cells = buckets[plat] ?? [];
            const spark = throughput[plat] ?? [];
            const sparkStroke =
              stats.tone === 'ok'
                ? 'var(--ok)'
                : stats.tone === 'warn'
                  ? 'var(--warn)'
                  : stats.tone === 'err'
                    ? 'var(--err)'
                    : 'var(--text-4)';
            return (
              <div className="pp-row" key={plat}>
                <button
                  type="button"
                  className="pp-meta pp-meta-btn"
                  aria-label={`Open ${capitalize(plat)} detail`}
                  onClick={(e) => {
                    openerRef.current = e.currentTarget;
                    setSelected(plat);
                  }}
                >
                  <span className="pp-name">{capitalize(plat)}</span>
                  <Chip
                    tone={stats.tone}
                    dot
                    {...(stats.tooltip === undefined ? {} : { title: stats.tooltip })}
                  >
                    {stats.label}
                  </Chip>
                  <span className="pp-stats">
                    {stats.lastSync} · {stats.errPct.toFixed(1)}% err · {stats.credsLabel}
                  </span>
                  <span className="pp-spark" aria-hidden="true">
                    <Sparkline values={spark} width={72} height={18} stroke={sparkStroke} />
                  </span>
                </button>
                {cells.map((cell, i) => (
                  <HeatCell key={i} tone={cell.tone} dim={cell.dim} title={cell.label} />
                ))}
              </div>
            );
          })}
        </div>
        <div className="pp-foot">
          <span />
          <span className="pp-foot-cells">
            <span>24h ago</span>
            <span>now</span>
          </span>
        </div>
      </CardBody>
      {selected !== null && (
        <PlatformDrillDrawer
          platform={selected}
          syncs={syncs}
          credentials={credentials}
          probe={probeByPlatform.get(selected)}
          onClose={closeDrawer}
        />
      )}
    </Card>
  );
}

function Legend(): ReactElement {
  return (
    <div className="row gap-3">
      <span className="legend">
        <span className="sw ok" />
        ok
      </span>
      <span className="legend">
        <span className="sw warn" />
        warn
      </span>
      <span className="legend">
        <span className="sw err" />
        err
      </span>
      <span className="legend">
        <span className="sw idle" />
        idle
      </span>
    </div>
  );
}

function computeRowStats(
  name: KnownPlatform,
  syncs: readonly SyncHistoryItem[],
  probe: ProbeState | undefined,
  credentials: readonly CredentialSummary[],
): RowStats {
  const scoped = syncs.filter((s) => s.platform === name);
  const hourMs = 60 * 60 * 1000;
  const since = Date.now() - hourMs;
  const recent = scoped.filter((s) => {
    const t = s.started_at !== null ? new Date(s.started_at).getTime() : 0;
    return t >= since;
  });
  const errs = recent.filter((s) => s.state === 'failed' || s.error !== null);
  const errPct = recent.length === 0 ? 0 : (errs.length / recent.length) * 100;

  // Daemon writes 'complete'; keep 'completed' as an alias so WS-sourced
  // rows that passed through older mappings still match.
  const lastOk = scoped.find(
    (s) => (s.state === 'complete' || s.state === 'completed') && s.finished_at !== null,
  );
  const lastSync =
    lastOk !== undefined && lastOk.finished_at !== null ? formatRelative(lastOk.finished_at) : '—';

  // Adapter probe is the source of truth for the chip. Sync-derived err rate
  // only shapes the tone within the healthy case so we never show "healthy"
  // on a platform that can't actually connect.
  const errTone: ChipTone = errPct >= 10 ? 'err' : errPct >= 3 ? 'warn' : 'ok';

  let tone: ChipTone;
  let label: string;
  let tooltip: string | undefined;
  if (probe === undefined) {
    tone = 'idle';
    label = 'probing…';
  } else if (!probe.enabled) {
    tone = 'idle';
    label = 'disabled';
  } else if (probe.healthy === true) {
    tone = errTone;
    label = errTone === 'ok' ? 'healthy' : errTone === 'warn' ? 'degraded' : 'failing';
  } else if (probe.healthy === false) {
    tone = 'err';
    label = 'unhealthy';
    tooltip = probe.error ?? undefined;
  } else {
    tone = 'idle';
    label = 'not configured';
    tooltip = probe.error ?? undefined;
  }
  return {
    tone,
    label,
    tooltip,
    lastSync,
    errPct,
    credsLabel: credentialsLabel(name, credentials),
  };
}

function credentialsLabel(name: KnownPlatform, credentials: readonly CredentialSummary[]): string {
  // Telegram is a singleton bot — its sentinel row is filtered out of
  // /api/credentials, so the user-cred count is always 0 for it. Label
  // it by what it actually is instead of misreading as "broken".
  if (name === 'telegram') return 'bot';
  const n = credentials.filter((c) => c.platform === name && !c.is_expired && !c.is_paused).length;
  return `${n} cred${n === 1 ? '' : 's'}`;
}

function hourlyThroughputByPlatform(syncs: readonly SyncHistoryItem[]): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  for (const p of KNOWN_PLATFORMS) out[p] = Array.from({ length: 24 }, () => 0);
  const now = Date.now();
  const hourMs = 60 * 60 * 1000;
  for (const s of syncs) {
    const bucket = out[s.platform];
    if (bucket === undefined) continue;
    const t = s.started_at !== null ? new Date(s.started_at).getTime() : NaN;
    if (Number.isNaN(t)) continue;
    const hoursAgo = Math.floor((now - t) / hourMs);
    if (hoursAgo < 0 || hoursAgo >= 24) continue;
    const idx = 23 - hoursAgo;
    const stats = s.stats as Record<string, unknown> | null;
    if (stats !== null) {
      const v = stats['items_fetched'] ?? stats['items_new'];
      if (typeof v === 'number') {
        bucket[idx] = (bucket[idx] ?? 0) + v;
      }
    }
  }
  return out;
}
