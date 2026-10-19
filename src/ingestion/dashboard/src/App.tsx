import { useEffect, useState, type ReactElement } from 'react';
import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { onUnauthorized } from './api/client';
import { Toasts } from './components/Toasts';
import { toast } from './lib/toasts';
import { getErrorMessage } from './lib/errors';
import { useWsCacheSync } from './ws/useWsCacheSync';

function mutationLabel(key: readonly unknown[] | undefined): string | undefined {
  if (key === undefined || key.length === 0) return undefined;
  const head = key[0];
  return typeof head === 'string' ? head : undefined;
}

export function App(): ReactElement {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
          mutations: { retry: 0 },
        },
        mutationCache: new MutationCache({
          onError: (error, _vars, _ctx, mutation) => {
            // 401 is handled by onUnauthorized → login redirect; don't toast.
            if (error instanceof Error && error.message.includes('401 ')) return;
            const label = mutationLabel(mutation.options.mutationKey) ?? 'Action failed';
            toast.error(label, getErrorMessage(error));
          },
          onSuccess: (_data, _vars, _ctx, mutation) => {
            const label = mutationLabel(mutation.options.mutationKey);
            if (label !== undefined) toast.success(label);
          },
        }),
      }),
  );

  useEffect(() => {
    return onUnauthorized(() => {
      const { pathname, search } = window.location;
      if (pathname === '/login') return;
      const next = encodeURIComponent(pathname + search);
      void router.navigate(`/login?next=${next}`, { replace: true });
    });
  }, []);

  return (
    <QueryClientProvider client={client}>
      <WsCacheSync />
      <RouterProvider router={router} />
      <Toasts />
    </QueryClientProvider>
  );
}

// Subscribes to the daemon's WS channels and invalidates React Query
// caches on every sync/error event so KPIs and other query-driven widgets
// stop going stale. Renders nothing. Must live inside QueryClientProvider.
function WsCacheSync(): null {
  useWsCacheSync();
  return null;
}
