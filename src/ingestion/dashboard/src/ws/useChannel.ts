import { useEffect, useRef, useSyncExternalStore } from 'react';
import { subscribe as subscribeToChannel } from './manager';
import type { ChannelStatus, EventFor, Filter, FilterFor } from './types';

interface UseChannelResult {
  status: ChannelStatus;
}

/**
 * Subscribe to a daemon WebSocket channel.
 *
 * Generic inference gives you a typed event callback based on the path:
 *   useChannel('/ws/syncs', (e) => { ... })  // e is SyncsEvent
 *   useChannel('/ws/logs',  (e) => { ... })  // e is LogEvent
 *
 * Unknown paths fall back to Record<string, unknown>.
 */
export function useChannel<P extends string>(
  path: P,
  onMessage: (event: EventFor<P>) => void,
  filter: FilterFor<P> | null = null,
): UseChannelResult {
  const handlerRef = useRef(onMessage);
  useEffect(() => {
    handlerRef.current = onMessage;
  }, [onMessage]);

  const subRef = useRef<ReturnType<typeof subscribeToChannel> | null>(null);
  const filterKey = JSON.stringify(filter);

  useEffect(() => {
    const sub = subscribeToChannel(path, filter as Filter | null, (event) => {
      handlerRef.current(event as EventFor<P>);
    });
    subRef.current = sub;
    return () => {
      sub.unsubscribe();
      subRef.current = null;
    };
    // filter serialises into filterKey; path + filterKey is the stable dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, filterKey]);

  const status = useSyncExternalStore(
    (notify) => {
      const sub = subRef.current;
      if (!sub) return () => undefined;
      return sub.subscribeStatus(() => notify());
    },
    () => subRef.current?.getStatus() ?? 'connecting',
    () => 'connecting' as const,
  );

  return { status };
}
