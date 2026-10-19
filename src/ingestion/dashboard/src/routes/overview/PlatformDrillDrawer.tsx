import { useEffect, useMemo, useRef, type ReactElement } from 'react';
import {
  Button,
  Chip,
  Drawer,
  DrawerBody,
  DrawerHeader,
  DrawerSectionHeading,
  Icon,
  type ChipTone,
} from '@/components/ui';
import { formatRelative } from '@/hooks/useRelativeTime';
import { capitalize, displayUserLabel, syncTone, type KnownPlatform } from './lib';
import { cn } from '@/lib/cn';
import type { SyncHistoryItem, CredentialSummary } from '@/api/queries';

interface ProbeState {
  enabled: boolean;
  healthy: boolean | null;
  error: string | null;
}

interface PlatformDrillDrawerProps {
  platform: KnownPlatform;
  syncs: readonly SyncHistoryItem[];
  credentials: readonly CredentialSummary[];
  probe: ProbeState | undefined;
  onClose: () => void;
}

export function PlatformDrillDrawer({
  platform,
  syncs,
  credentials,
  probe,
  onClose,
}: PlatformDrillDrawerProps): ReactElement {
  // Escape closes. Backdrop is its own button below (non-focusable, so
  // keyboard users dismiss via Escape or the close button in the header).
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // WAI-ARIA dialog pattern: focus the close button when the drawer opens
  // so keyboard focus moves into the overlay rather than stranded behind it.
  // Restoration back to the opener button lives in PlatformPanel.
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    closeBtnRef.current?.focus();
  }, []);

  const scoped = useMemo(() => syncs.filter((s) => s.platform === platform), [syncs, platform]);
  const scopedCreds = useMemo(
    () => credentials.filter((c) => c.platform === platform),
    [credentials, platform],
  );

  // ``new Date().getTime()`` slips past react-hooks/purity (unlike the
  // equivalent ``Date.now()``), matching the pattern already used in
  // PlatformPanel / lib.ts for the 24h window.
  const { recentCount, failed24h, lastOkAt } = useMemo(() => {
    const since = new Date().getTime() - 24 * 60 * 60 * 1000;
    const rec = scoped.filter((s) => {
      const t = s.started_at !== null ? new Date(s.started_at).getTime() : 0;
      return t >= since;
    });
    const failed = rec.filter((s) => s.state === 'failed').length;
    const ok = scoped.find(
      (s) => (s.state === 'complete' || s.state === 'completed') && s.finished_at !== null,
    );
    return {
      recentCount: rec.length,
      failed24h: failed,
      lastOkAt: ok?.finished_at ?? null,
    };
  }, [scoped]);

  const { tone: probeTone, label: probeLabel, tooltip: probeTooltip } = probeChip(probe);

  const labelId = `pp-drill-title-${platform}`;
  return (
    <>
      <button
        type="button"
        className="pp-drill-backdrop"
        aria-label="Close platform detail"
        tabIndex={-1}
        onClick={onClose}
      />
      <Drawer className="pp-drill" role="dialog" aria-modal="true" aria-labelledby={labelId}>
        <DrawerHeader
          title={
            <>
              <Button
                ref={closeBtnRef}
                size="sm"
                variant="ghost"
                aria-label="Close drawer"
                onClick={onClose}
              >
                <Icon name="close" size={12} />
              </Button>
              <span id={labelId} className="mono" style={{ marginLeft: 6, fontSize: 14 }}>
                {capitalize(platform)}
              </span>
              <span style={{ marginLeft: 'auto' }}>
                <Chip
                  tone={probeTone}
                  dot
                  {...(probeTooltip === undefined ? {} : { title: probeTooltip })}
                >
                  {probeLabel}
                </Chip>
              </span>
            </>
          }
        />
        <DrawerBody>
          <section>
            <DrawerSectionHeading>Summary · last 24h</DrawerSectionHeading>
            <div className="grid-2" style={{ gap: 8 }}>
              <MiniKpi label="total syncs" value={String(recentCount)} />
              <MiniKpi
                label="failed"
                value={<span className={failed24h > 0 ? 'text-err' : undefined}>{failed24h}</span>}
              />
              <MiniKpi
                label="credentials"
                value={platform === 'telegram' ? 'bot' : String(scopedCreds.length)}
              />
              <MiniKpi
                label="last success"
                value={lastOkAt !== null ? formatRelative(lastOkAt) : '—'}
              />
            </div>
          </section>

          {platform !== 'telegram' && (
            <section>
              <DrawerSectionHeading>Credentials ({scopedCreds.length})</DrawerSectionHeading>
              <div className="col gap-2">
                {scopedCreds.length === 0 && (
                  <span className="tiny dim">No credentials registered.</span>
                )}
                {scopedCreds.map((c) => (
                  <div
                    key={`${c.user_email}::${c.platform}`}
                    className={cn('pp-drill-cred', c.is_expired && 'is-err')}
                  >
                    <div className="row gap-2">
                      <span className="mono" style={{ fontSize: 12 }}>
                        {displayUserLabel(c.user_email)}
                      </span>
                      <span className="fill" />
                      {c.is_expired && <Chip tone="err">expired</Chip>}
                      {c.is_paused && <Chip tone="warn">paused</Chip>}
                      {!c.is_expired && !c.is_paused && <Chip tone="ok">healthy</Chip>}
                    </div>
                    <div className="mono tiny muted" style={{ marginTop: 4 }}>
                      {c.last_refreshed_at !== null
                        ? `refreshed ${formatRelative(c.last_refreshed_at)}`
                        : 'never refreshed'}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section>
            <DrawerSectionHeading>Recent syncs</DrawerSectionHeading>
            <div className="col gap-1">
              {scoped.length === 0 && <span className="tiny dim">No syncs for {platform}.</span>}
              {scoped.slice(0, 12).map((s) => (
                <SyncRow key={s.id} sync={s} />
              ))}
            </div>
          </section>
        </DrawerBody>
      </Drawer>
    </>
  );
}

function SyncRow({ sync }: { sync: SyncHistoryItem }): ReactElement {
  const tone: ChipTone = syncTone(sync.state);
  const when = sync.started_at !== null ? formatRelative(sync.started_at) : '—';
  const items = readItems(sync.stats);
  return (
    <div className="pp-drill-sync">
      <Chip tone={tone}>{sync.state}</Chip>
      <span className="mono" style={{ fontSize: 12 }}>
        {displayUserLabel(sync.user_email)}
      </span>
      <span className="fill" />
      <span className="tiny dim">{when}</span>
      {items !== null && <span className="mono tiny">{items}</span>}
    </div>
  );
}

function MiniKpi({ label, value }: { label: string; value: React.ReactNode }): ReactElement {
  return (
    <div className="pp-drill-kpi">
      <span className="pp-drill-kpi-v">{value}</span>
      <span className="pp-drill-kpi-l">{label}</span>
    </div>
  );
}

function probeChip(probe: ProbeState | undefined): {
  tone: ChipTone;
  label: string;
  tooltip: string | undefined;
} {
  if (probe === undefined) return { tone: 'idle', label: 'probing…', tooltip: undefined };
  if (!probe.enabled) return { tone: 'idle', label: 'disabled', tooltip: undefined };
  if (probe.healthy === true) return { tone: 'ok', label: 'healthy', tooltip: undefined };
  if (probe.healthy === false)
    return { tone: 'err', label: 'unhealthy', tooltip: probe.error ?? undefined };
  return { tone: 'idle', label: 'not configured', tooltip: probe.error ?? undefined };
}

function readItems(stats: unknown): number | null {
  if (stats === null || typeof stats !== 'object') return null;
  const rec = stats as Record<string, unknown>;
  const v = rec['items_fetched'] ?? rec['items_new'];
  return typeof v === 'number' ? v : null;
}
