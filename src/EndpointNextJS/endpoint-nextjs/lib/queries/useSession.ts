"use client";

import { ApiError } from "@/lib/api/client";

import { useConnections } from "./useConnections";

export function useSession() {
  const query = useConnections();
  const isAnonymous = query.error instanceof ApiError && query.error.status === 401;

  return {
    isAuthed: query.isSuccess,
    isResolved: query.isSuccess || isAnonymous,
  };
}
