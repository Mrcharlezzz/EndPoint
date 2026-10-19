"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchPreferences, setPreferences } from "@/lib/api/preferences";
import type { ApiPreferences } from "@/lib/api/types";

import { queryKeys } from "./keys";

export function usePreferences(enabled = true) {
  return useQuery<ApiPreferences>({
    queryKey: queryKeys.preferences(),
    queryFn: fetchPreferences,
    staleTime: 60_000,
    enabled,
    retry: false,
  });
}

export function useSetPreferences() {
  const qc = useQueryClient();
  return useMutation<ApiPreferences, unknown, ApiPreferences, { previous: ApiPreferences | undefined }>({
    mutationFn: setPreferences,
    onMutate: async (next) => {
      await qc.cancelQueries({ queryKey: queryKeys.preferences() });
      const previous = qc.getQueryData<ApiPreferences>(queryKeys.preferences());
      qc.setQueryData<ApiPreferences>(queryKeys.preferences(), {
        ...(previous ?? {}),
        ...next,
      });
      return { previous };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.previous) qc.setQueryData(queryKeys.preferences(), ctx.previous);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.preferences() });
    },
    meta: { errorToast: "Couldn't save preferences." },
  });
}
