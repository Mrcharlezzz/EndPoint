import { useMemo, useState, type ReactElement } from 'react';
import { Button } from '@/components/ui/Button';
import { Chip } from '@/components/ui/Chip';
import { ConfirmModal } from '@/components/ui/ConfirmModal';
import { Toggle } from '@/components/ui/Toggle';
import { getErrorMessage } from '@/lib/errors';
import { toast } from '@/lib/toasts';
import {
  useConfig,
  useCredentials,
  useFullResync,
  type CredentialSummary,
  type FullResyncPlatform,
} from '@/api/queries';

// Telegram is a singleton bot — not per-user content — and is deliberately
// excluded from this per-user-credential fan-out.
const ALL_PLATFORMS: readonly FullResyncPlatform[] = ['teams', 'moodle'] as const;

const PLATFORM_LABEL: Record<FullResyncPlatform, string> = {
  teams: 'Teams',
  moodle: 'Moodle',
};

interface PlatformCounts {
  total: number;
  expired: number;
  paused: number;
  willSync: number;
}

export function FullResyncButton(): ReactElement {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Set<FullResyncPlatform>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const config = useConfig();
  const credentials = useCredentials();
  const resync = useFullResync();

  const pausedSet = useMemo(() => derivePaused(config.data), [config.data]);
  const counts = useMemo(() => countsByPlatform(credentials.data ?? []), [credentials.data]);
  // Don't surface zero-valued counts when the credentials query hasn't
  // resolved — "0 will sync" reads as "nothing to do" when the truth is
  // "I don't know yet".
  const countsStatus: 'ok' | 'loading' | 'error' = credentials.isError
    ? 'error'
    : credentials.data === undefined
      ? 'loading'
      : 'ok';

  function openModal(): void {
    setSelected(new Set(ALL_PLATFORMS.filter((p) => !pausedSet.has(p))));
    setError(null);
    setOpen(true);
  }

  function toggle(platform: FullResyncPlatform): void {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(platform)) {
        next.delete(platform);
      } else {
        next.add(platform);
      }
      return next;
    });
  }

  function close(): void {
    if (resync.isPending) return;
    setOpen(false);
    setError(null);
  }

  async function submit(reason?: string): Promise<void> {
    setError(null);
    const platforms = ALL_PLATFORMS.filter((p) => selected.has(p));
    if (platforms.length === 0) {
      setError('Pick at least one platform.');
      return;
    }
    if (reason === undefined || reason.length === 0) {
      setError('A reason is required.');
      return;
    }
    try {
      const result = await resync.mutateAsync({ reason, platforms });
      const skippedNote =
        result.credentials_skipped > 0 ? ` · ${result.credentials_skipped} expired skipped` : '';
      toast.success(
        'Full resync queued',
        `${result.syncs_enqueued} syncs across ${result.platforms.join(', ')}${skippedNote}`,
      );
      setOpen(false);
    } catch (err: unknown) {
      setError(getErrorMessage(err));
    }
  }

  const totalWillSync = ALL_PLATFORMS.filter((p) => selected.has(p)).reduce(
    (acc, p) => acc + (counts[p]?.willSync ?? 0),
    0,
  );

  return (
    <>
      <Button variant="danger" onClick={openModal}>
        Full Resync
      </Button>
      <ConfirmModal
        open={open}
        variant="danger"
        title="Full Resync"
        body={
          <div className="col gap-2">
            <div className="tiny muted">
              Wipes sync cursors for the selected platforms and re-ingests every non-expired
              credential from scratch. Expect heavy load on upstream APIs.
            </div>
            <div className="col gap-2">
              <span className="tiny muted">Platforms</span>
              {ALL_PLATFORMS.map((platform) => {
                const isPaused = pausedSet.has(platform);
                const c = counts[platform] ?? { total: 0, expired: 0, paused: 0, willSync: 0 };
                const isSelected = selected.has(platform);
                return (
                  <div key={platform} className="row gap-2 fr-platform-row">
                    <Toggle
                      checked={isSelected}
                      onChange={() => toggle(platform)}
                      ariaLabel={`Include ${PLATFORM_LABEL[platform]} in resync`}
                    />
                    <span className="fr-platform-label">{PLATFORM_LABEL[platform]}</span>
                    {isPaused && <Chip tone="warn">paused</Chip>}
                    <span className="fill" />
                    <span className="mono tiny dim fr-platform-counts">
                      {countsStatus === 'loading' ? (
                        'counts loading…'
                      ) : countsStatus === 'error' ? (
                        'counts unavailable'
                      ) : (
                        <>
                          {c.total} creds · {c.expired} expired · {c.paused} paused ·{' '}
                          <span
                            className={isSelected && c.willSync > 0 ? 'mono text-ok' : 'mono dim'}
                          >
                            {c.willSync} will sync
                          </span>
                        </>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
            {countsStatus === 'ok' && totalWillSync > 0 && (
              <div className="tiny muted" style={{ marginTop: 4 }}>
                Total impact: <span className="mono text-ok">{totalWillSync} syncs</span> will be
                queued.
              </div>
            )}
          </div>
        }
        typedConfirm="FULL_RESYNC"
        requireReason
        confirmLabel="Queue resync"
        onConfirm={submit}
        onCancel={close}
        busy={resync.isPending}
        error={error}
      />
    </>
  );
}

function derivePaused(config: ReturnType<typeof useConfig>['data']): Set<FullResyncPlatform> {
  const paused = new Set<FullResyncPlatform>();
  const cfg = config?.platforms?.paused;
  if (cfg === undefined) return paused;
  if (cfg.teams) paused.add('teams');
  if (cfg.moodle) paused.add('moodle');
  return paused;
}

function isFullResyncPlatform(p: string): p is FullResyncPlatform {
  return p === 'teams' || p === 'moodle';
}

function countsByPlatform(
  credentials: readonly CredentialSummary[],
): Record<FullResyncPlatform, PlatformCounts> {
  const out: Record<FullResyncPlatform, PlatformCounts> = {
    teams: { total: 0, expired: 0, paused: 0, willSync: 0 },
    moodle: { total: 0, expired: 0, paused: 0, willSync: 0 },
  };
  for (const c of credentials) {
    if (!isFullResyncPlatform(c.platform)) continue;
    const bucket = out[c.platform];
    bucket.total += 1;
    if (c.is_expired) bucket.expired += 1;
    if (c.is_paused) bucket.paused += 1;
    if (!c.is_expired && !c.is_paused) bucket.willSync += 1;
  }
  return out;
}
