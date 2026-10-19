import { useEffect, useMemo, useState, type ReactElement } from 'react';
import { LiveChip } from '@/components/LiveChip';
import { useCredentials, useErrors, useSyncs } from '@/api/queries';
import { StatusStrip } from './StatusStrip';
import { PlatformPanel } from './PlatformPanel';
import { ActivityFeed } from './ActivityFeed';
import { ErrorsFeed } from './ErrorsFeed';
import { FullResyncButton } from './FullResyncButton';
import { NextSyncPanel } from './NextSyncPanel';
import { OverviewMetrics } from './OverviewMetrics';
import { ProcessorSummaryPanel } from './ProcessorSummaryPanel';
import '@/styles/pages/overview.css';

export function OverviewPage(): ReactElement {
  const syncs = useSyncs({ limit: 200 });
  const errors = useErrors({ limit: 20 });
  const credentials = useCredentials();

  // Keep a ticking clock for header stats that depend on "today" rather than
  // on sync events (otherwise items-today stays stale across midnight on a
  // quiet day).
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, []);

  const { itemsToday, errRatePct } = useMemo(() => {
    const s = syncs.data ?? [];
    const startOfDay = new Date(nowMs);
    startOfDay.setHours(0, 0, 0, 0);
    let itemsToday = 0;
    for (const row of s) {
      if (row.started_at === null) continue;
      if (new Date(row.started_at).getTime() < startOfDay.getTime()) continue;
      const stats = row.stats as Record<string, unknown> | null;
      if (stats !== null) {
        const fetched = stats['items_fetched'] ?? stats['items_new'];
        if (typeof fetched === 'number') itemsToday += fetched;
      }
    }
    // Error rate computed from the recent syncs window instead of
    // cumulative-failed / active-plus-queued (which produced nonsense like
    // 300% when failures outpaced the current in-flight denominator).
    const failed = s.filter((r) => r.state === 'failed').length;
    const errRatePct = s.length > 0 ? (failed / s.length) * 100 : 0;
    return { itemsToday, errRatePct };
  }, [syncs.data, nowMs]);

  return (
    <>
      <header className="page-header">
        <h1>Overview</h1>
        <span className="crumb">· live system state</span>
        <span className="spacer" />
        <FullResyncButton />
        <span className="meta">
          <LiveChip />
          <OverviewMetrics itemsToday={itemsToday} errRatePct={errRatePct} />
          <span className="sep">·</span>
          <span>last 24 hours</span>
        </span>
      </header>

      <div className="page-body">
        <StatusStrip />

        <ProcessorSummaryPanel />

        <PlatformPanel syncs={syncs.data ?? []} credentials={credentials.data ?? []} />

        <NextSyncPanel syncs={syncs.data ?? []} />

        <div className="grid-2 dual-feeds">
          <ActivityFeed initial={syncs.data ?? []} />
          <ErrorsFeed initial={errors.data} />
        </div>
      </div>
    </>
  );
}
