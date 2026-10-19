import "client-only";

import { type Mutation, MutationCache, type Query, QueryCache, QueryClient } from "@tanstack/react-query";

import { log } from "@/lib/logger";

type ErrorMeta = { errorToast?: false | string };

type AnyQuery = Query<unknown, unknown, unknown, readonly unknown[]>;
type AnyMutation = Mutation<unknown, unknown, unknown, unknown>;

function readErrorMeta(meta: unknown): ErrorMeta {
  if (meta && typeof meta === "object" && "errorToast" in meta) {
    return meta as ErrorMeta;
  }
  return {};
}

export type QueryClientHandlers = {
  /**
   * Called when a query throws. The handler decides whether to surface the
   * error to the user (e.g. toast). The query layer itself stays free of
   * UI dependencies.
   */
  onQueryError?: (args: {
    error: unknown;
    query: AnyQuery;
    /** True for background refetches when stale data is already on screen. */
    isBackgroundRefetch: boolean;
    meta: ErrorMeta;
  }) => void;

  onMutationError?: (args: {
    error: unknown;
    mutation: AnyMutation;
    meta: ErrorMeta;
  }) => void;
};

/**
 * Build a QueryClient. Called once per browser session inside the Providers
 * client component (via useState) so the cache is never shared across SSR
 * requests or React renders.
 *
 * `handlers` is the seam between the cache and the UI: pass in toast / log
 * callbacks from the wiring layer (app/providers) so this module stays
 * within the lib/api boundary.
 */
export function makeQueryClient(handlers: QueryClientHandlers = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
      mutations: {
        retry: 0,
      },
    },
    queryCache: new QueryCache({
      onError: (error, query) => {
        log.error("query.error", error, { queryKey: query.queryKey });
        handlers.onQueryError?.({
          error,
          query,
          isBackgroundRefetch: query.state.data !== undefined,
          meta: readErrorMeta(query.meta),
        });
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => {
        log.error("mutation.error", error, { mutationKey: mutation.options.mutationKey });
        handlers.onMutationError?.({
          error,
          mutation,
          meta: readErrorMeta(mutation.meta),
        });
      },
    }),
  });
}
