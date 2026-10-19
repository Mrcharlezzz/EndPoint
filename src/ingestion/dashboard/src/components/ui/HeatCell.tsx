import type { ReactElement } from 'react';
import { cn } from '@/lib/cn';

export type HeatTone = 'ok' | 'warn' | 'err' | 'idle';

interface HeatCellProps {
  tone?: HeatTone;
  /** Saturation tier for 'ok'; absent = full saturation. */
  dim?: 1 | 2 | 3 | undefined;
  title?: string;
  onClick?: () => void;
}

export function HeatCell({ tone = 'idle', dim, title, onClick }: HeatCellProps): ReactElement {
  const dimClass = dim !== undefined ? `d${dim}` : undefined;
  return (
    <div
      className={cn('heat-cell', tone, tone === 'ok' && dimClass)}
      title={title}
      onClick={onClick}
      role={onClick !== undefined ? 'button' : undefined}
      tabIndex={onClick !== undefined ? 0 : undefined}
    />
  );
}
