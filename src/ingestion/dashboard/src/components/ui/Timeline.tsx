import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export type TimelineTone = 'info' | 'ok' | 'warn' | 'err' | 'idle';

export interface TimelineItem {
  key: string;
  time: ReactNode;
  tone?: TimelineTone;
  body: ReactNode;
}

interface TimelineProps {
  items: readonly TimelineItem[];
  variant?: 'default' | 'audit';
  className?: string;
}

export function Timeline({ items, variant = 'default', className }: TimelineProps): ReactElement {
  return (
    <div className={cn('timeline', variant === 'audit' && 'audit-tl', className)}>
      {items.map((item) => (
        <div key={item.key} className="tl-item">
          <span className="tl-time">{item.time}</span>
          <span className={cn('tl-dot', item.tone && `k-${item.tone}`)} />
          <span className="tl-body">{item.body}</span>
        </div>
      ))}
    </div>
  );
}
