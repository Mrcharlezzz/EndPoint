import type { ReactElement } from 'react';
import { Link } from 'react-router-dom';
import { Chip, type ChipTone } from '@/components/ui';

interface OverviewMetricsProps {
  itemsToday: number;
  errRatePct: number;
}

export function OverviewMetrics({ itemsToday, errRatePct }: OverviewMetricsProps): ReactElement {
  const errTone: ChipTone = errRatePct >= 10 ? 'err' : errRatePct >= 3 ? 'warn' : 'ok';
  return (
    <>
      <span className="sep">·</span>
      <span className="tiny dim">items today</span>
      <Link
        to="/syncs"
        className="mono metric-link"
        title="Open Syncs"
        aria-label={`${formatThousands(itemsToday)} items ingested today — open Syncs`}
      >
        {formatThousands(itemsToday)}
      </Link>
      <span className="sep">·</span>
      <span className="tiny dim">err rate</span>
      <Link
        to="/errors?filter=failed_sync"
        className="metric-link"
        title="Open Errors · failed syncs"
        aria-label={`Error rate ${errRatePct.toFixed(1)} percent — open failed syncs`}
      >
        <Chip tone={errTone}>{errRatePct.toFixed(1)}%</Chip>
      </Link>
    </>
  );
}

function formatThousands(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
