import { useState, type ReactElement } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Icon,
  Input,
  PlatformChip,
  Toggle,
} from '@/components/ui';
import {
  useClearConfigOverlay,
  useConfig,
  useConfigOverlay,
  useSetConfigOverlay,
  type DaemonConfig,
} from '@/api/queries';
import '@/styles/pages/config.css';

type Platform = 'teams' | 'moodle' | 'telegram';

interface PlatformSpec {
  key: Platform;
  pausedKey: string;
}

type LiveIngestionConfig = DaemonConfig;

const PLATFORMS: readonly PlatformSpec[] = [
  { key: 'teams', pausedKey: 'platforms.paused.teams' },
  { key: 'moodle', pausedKey: 'platforms.paused.moodle' },
  { key: 'telegram', pausedKey: 'platforms.paused.telegram' },
];

export function ConfigPage(): ReactElement {
  const config = useConfig();
  const overlay = useConfigOverlay();
  const effective = config.data;
  const overlayCount = Object.keys(overlay.data ?? {}).length;

  return (
    <>
      <header className="page-header">
        <h1>Config</h1>
        <span className="crumb">· live ingestion controls</span>
        <span className="spacer" />
        <span className="meta">
          <Chip tone={overlayCount > 0 ? 'info' : 'idle'}>{overlayCount} overlay keys</Chip>
        </span>
      </header>

      <div className="page-body config-page">
        <Card>
          <CardHeader
            title="Ingestion filters"
            sub="control the recency window and pause individual platform syncs"
          />
          <CardBody>
            <RecencyEditor
              label="Recency window (days)"
              overlayKey="ingestion_controls.recency_days"
              value={effective?.ingestion_controls?.recency_days ?? null}
            />
            <div className="config-platform-list">
              {PLATFORMS.map((platform) => (
                <PlatformPauseRow key={platform.key} spec={platform} config={effective} />
              ))}
            </div>
          </CardBody>
        </Card>
      </div>
    </>
  );
}

function PlatformPauseRow({
  spec,
  config,
}: {
  spec: PlatformSpec;
  config: LiveIngestionConfig | undefined;
}): ReactElement {
  const setOverlay = useSetConfigOverlay();
  const clearOverlay = useClearConfigOverlay();
  const paused = getPlatformPaused(config, spec);
  const busy = setOverlay.isPending || clearOverlay.isPending;

  return (
    <div className="config-platform-row">
      <div className="config-platform-copy">
        <PlatformChip name={spec.key} />
        <div className="config-platform-state">
          {paused ? 'paused for future syncs' : 'active for future syncs'}
        </div>
      </div>
      <div className="config-platform-actions">
        <Chip tone={paused ? 'warn' : 'ok'}>{paused ? 'paused' : 'active'}</Chip>
        <Toggle
          checked={paused}
          onChange={(next) => {
            if (busy) return;
            if (next) {
              void setOverlay.mutateAsync({ key: spec.pausedKey, value: true });
              return;
            }
            void clearOverlay.mutateAsync({ key: spec.pausedKey });
          }}
          ariaLabel={`${paused ? 'Resume' : 'Pause'} ${spec.key} ingestion`}
        />
      </div>
    </div>
  );
}

function RecencyEditor({
  label,
  overlayKey,
  value,
}: {
  label: string;
  overlayKey: string;
  value: number | null;
}): ReactElement {
  const [draft, setDraft] = useState<string | null>(null);
  const setOverlay = useSetConfigOverlay();
  const clearOverlay = useClearConfigOverlay();
  const currentValue = value === null ? '' : String(value);
  const inputValue = draft ?? currentValue;
  const parsed = parsePositiveInt(inputValue);
  const canApply = parsed !== null && parsed !== value && setOverlay.isPending !== true;
  const busy = setOverlay.isPending || clearOverlay.isPending;

  return (
    <div className="config-recency-row">
      <label>
        <span>{label}</span>
        <Input
          value={inputValue}
          inputMode="numeric"
          placeholder="90"
          disabled={busy}
          onChange={(event) => setDraft(event.currentTarget.value)}
        />
      </label>
      <Button
        size="sm"
        variant="primary"
        disabled={!canApply || busy}
        iconLeft={<Icon name="check" size={12} />}
        onClick={() => {
          if (parsed !== null) {
            void setOverlay.mutateAsync({ key: overlayKey, value: parsed }).then(() => {
              setDraft(null);
            });
          }
        }}
      >
        Apply
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={value === null || busy}
        iconLeft={<Icon name="close" size={12} />}
        onClick={() =>
          void clearOverlay.mutateAsync({ key: overlayKey }).then(() => {
            setDraft(null);
          })
        }
      >
        Clear
      </Button>
    </div>
  );
}

function getPlatformPaused(config: LiveIngestionConfig | undefined, spec: PlatformSpec): boolean {
  const paused = config?.platforms?.paused;
  if (paused === undefined) return false;
  const key = spec.key as keyof typeof paused;
  return paused[key] === true;
}

function parsePositiveInt(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed.length === 0) return null;
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return parsed >= 1 ? parsed : null;
}
