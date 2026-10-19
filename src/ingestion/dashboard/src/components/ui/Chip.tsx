import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export type ChipTone = 'ok' | 'warn' | 'err' | 'run' | 'info' | 'idle' | 'ghost' | 'neutral';

interface ChipProps {
  tone?: ChipTone;
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}

export function Chip({
  tone = 'neutral',
  dot = false,
  children,
  className,
  title,
}: ChipProps): ReactElement {
  const toneClass = tone === 'neutral' ? undefined : tone;
  return (
    <span className={cn('chip', toneClass, className)} title={title}>
      {dot && <span className="cdot" />}
      {children}
    </span>
  );
}
