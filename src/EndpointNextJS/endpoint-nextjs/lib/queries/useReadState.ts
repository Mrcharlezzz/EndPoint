"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { clearReadState, fetchReadState, markRead, type ReadStateMap } from "@/lib/api/readState";

import { queryKeys } from "./keys";

const STAR = queryKeys.readState();

/**
 * Returns the read-state map keyed by item id. UI consumers typically
 * derive `isRead = !!data[item.id]`.
 */
export function useReadState({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery<ReadStateMap>({
    queryKey: STAR,
    queryFn: fetchReadState,
    staleTime: 60_000,
    enabled,
  });
}

/**
 * Optimistic mark-read. Accepts a single id or a batch and patches the
 * cache before the request lands so cards update without a flash.
 */
export function useMarkRead() {
  const qc = useQueryClient();
  return useMutation<void, unknown, string[], { previous: ReadStateMap | undefined }>({
    mutationFn: markRead,
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey: STAR });
      const previous = qc.getQueryData<ReadStateMap>(STAR);
      const now = new Date().toISOString();
      qc.setQueryData<ReadStateMap>(STAR, (current) => {
        const next: ReadStateMap = { ...(current ?? {}) };
        for (const id of ids) next[id] = now;
        return next;
      });
      return { previous };
    },
    onError: (_err, _ids, ctx) => {
      if (ctx?.previous) qc.setQueryData(STAR, ctx.previous);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: STAR });
    },
    meta: { errorToast: "Couldn't mark as read." },
  });
}

export function useClearReadState() {
  const qc = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: clearReadState,
    onSuccess: () => {
      qc.setQueryData<ReadStateMap>(STAR, {});
    },
    meta: { errorToast: "Couldn't clear read history." },
  });
}
