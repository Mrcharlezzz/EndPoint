import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface FilterPillProps {
  active?: boolean;
  count?: number;
  countTone?: 'ok' | 'warn' | 'err';
  onClick?: () => void;
  children: ReactNode;
  className?: string;
}

export function FilterPill({
  active,
  count,
  countTone,
  onClick,
  children,
  className,
}: FilterPillProps): ReactElement {
  const countClass =
    count !== undefined && count > 0 && countTone !== undefined ? `count-${countTone}` : undefined;
  return (
    <button
      type="button"
      className={cn('pill', active && 'active', countClass, className)}
      onClick={onClick}
    >
      <span>{children}</span>
      {count !== undefined && <span className="n">{count}</span>}
    </button>
  );
}
