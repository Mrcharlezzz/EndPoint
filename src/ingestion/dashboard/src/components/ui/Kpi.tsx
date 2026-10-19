import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface KpiProps {
  label: ReactNode;
  value: ReactNode;
  unit?: ReactNode;
  delta?: ReactNode;
  deltaDirection?: 'up' | 'down' | 'flat';
  spark?: ReactNode;
  size?: 'default' | 'small' | 'mini';
  className?: string;
}

export function Kpi({
  label,
  value,
  unit,
  delta,
  deltaDirection,
  spark,
  size = 'default',
  className,
}: KpiProps): ReactElement {
  if (size === 'mini') {
    return (
      <div className={cn('kpi mini', className)}>
        <span className="label">{value}</span>
        {label !== undefined && <span className="sub">{label}</span>}
      </div>
    );
  }
  return (
    <div className={cn('kpi', size === 'small' && 'small', className)}>
      <div className="label">{label}</div>
      <div className="value">
        {value}
        {unit !== undefined && <span className="unit">{unit}</span>}
      </div>
      {(delta !== undefined || spark !== undefined) && (
        <div className="bottom">
          {delta !== undefined && (
            <span className={cn('delta', deltaDirection)}>{delta}</span>
          )}
          {spark !== undefined && <span className="spark">{spark}</span>}
        </div>
      )}
    </div>
  );
}
