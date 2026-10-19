"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  disconnectPlatform,
  fetchConnections,
  reconnectPlatform,
} from "@/lib/api/connections";
import type { ApiConnection } from "@/lib/api/types";

import { queryKeys } from "./keys";

type QueryOptions = {
  enabled?: boolean;
};

export function useConnections({ enabled = true }: QueryOptions = {}) {
  return useQuery<ApiConnection[]>({
    queryKey: queryKeys.connections(),
    queryFn: async () => {
      const data = await fetchConnections();
      return data.results;
    },
    enabled,
  });
}

export function useDisconnectPlatform() {
  const qc = useQueryClient();
  return useMutation<void, unknown, "moodle" | "teams">({
    mutationFn: disconnectPlatform,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.connections() });
    },
    meta: { errorToast: "Couldn't disconnect that platform." },
  });
}

export function useReconnectPlatform() {
  const qc = useQueryClient();
  return useMutation<void, unknown, "moodle" | "teams">({
    mutationFn: reconnectPlatform,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.connections() });
    },
    meta: { errorToast: "Couldn't reconnect." },
  });
}
