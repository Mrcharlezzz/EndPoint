import { Fragment } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface KVProps {
  rows: readonly (readonly [key: ReactNode, value: ReactNode])[];
  mono?: boolean;
  className?: string;
}

export function KV({ rows, mono, className }: KVProps): ReactElement {
  return (
    <div className={cn('kv', mono && 'mono', className)}>
      {rows.map((row, i) => (
        <Fragment key={i}>
          <span>{row[0]}</span>
          <span>{row[1]}</span>
        </Fragment>
      ))}
    </div>
  );
}
