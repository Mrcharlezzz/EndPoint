import type { ReactElement } from 'react';
import { cn } from '@/lib/cn';

export interface PhaseSegment {
  label: string;
  ms: number;
  tone?: 'ok' | 'info' | 'warn' | 'err';
}

interface PhaseTrackProps {
  segments: readonly PhaseSegment[];
}

export function PhaseTrack({ segments }: PhaseTrackProps): ReactElement {
  const total = segments.reduce((acc, s) => acc + Math.max(0, s.ms), 0) || 1;
  return (
    <>
      <div className="phase-track">
        {segments.map((seg, i) => {
          const pct = (Math.max(0, seg.ms) / total) * 100;
          return (
            <div
              key={i}
              className={cn('seg', seg.tone ?? 'info')}
              style={{ width: `${pct}%` }}
              title={`${seg.label} · ${seg.ms}ms`}
            >
              {seg.label}
            </div>
          );
        })}
      </div>
      <div className="phase-labels">
        {segments.map((seg, i) => {
          const pct = (Math.max(0, seg.ms) / total) * 100;
          return (
            <span key={i} style={{ width: `${pct}%` }}>
              {seg.ms}ms
            </span>
          );
        })}
      </div>
    </>
  );
}
