import { useQueryClient } from '@tanstack/react-query';
import { useChannel } from './useChannel';

/**
 * Keep React Query caches fresh when the daemon emits WS events.
 *
 * The WS-driven widgets (ActivityFeed, ErrorsFeed, LiveChip) merge live
 * events with their initial query data. The query data itself is what
 * PlatformPanel, StatusStrip, NextSyncPanel, and every other
 * component read from — and those queries never re-fetch on WS events
 * by default, so their props go stale.
 *
 * This hook closes the gap: when a sync lifecycle event lands on
 * ``/ws/syncs`` or a new error lands on ``/ws/errors``, it invalidates
 * the relevant query prefixes so every subscribed component re-fetches.
 * Mount once at the app root — the WS manager dedupes connections by
 * (path, filter), so mounting here doesn't double-subscribe pages that
 * also call ``useChannel`` directly.
 */
export function useWsCacheSync(): void {
  const qc = useQueryClient();

  useChannel('/ws/syncs', (event) => {
    // Prefix matching picks up every cached variant: syncs({limit:200}),
    // syncs({state:'running'}), syncs() — all share the 'syncs' prefix.
    // ``invalidateQueries`` returns a promise for the refetch; we don't
    // care about awaiting the refresh here.
    void qc.invalidateQueries({ queryKey: ['syncs'] });
    void qc.invalidateQueries({ queryKey: ['errors'] });
    void qc.invalidateQueries({ queryKey: ['credentials'] });
    // Next-sync countdown depends on ``last_success_at`` which just
    // advanced; refresh it so the timer resets when a sync lands.
    void qc.invalidateQueries({ queryKey: ['schedule'] });
    const user = typeof event.user_email === 'string' ? event.user_email : undefined;
    if (user !== undefined && user.length > 0) {
      void qc.invalidateQueries({ queryKey: ['users', user] });
    }
  });

  useChannel('/ws/errors', () => {
    void qc.invalidateQueries({ queryKey: ['errors'] });
    void qc.invalidateQueries({ queryKey: ['syncs'] });
  });
}
