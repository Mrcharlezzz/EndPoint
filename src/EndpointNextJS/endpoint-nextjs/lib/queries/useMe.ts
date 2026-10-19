"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { logout } from "@/lib/api/auth";
import { deleteMe, fetchMe } from "@/lib/api/users";
import type { AuthApiUser } from "@/lib/api/types";

import { queryKeys } from "./keys";

export function useMe() {
  return useQuery<AuthApiUser>({
    queryKey: queryKeys.me(),
    queryFn: fetchMe,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: logout,
    onSuccess: () => {
      qc.clear();
    },
    meta: { errorToast: "Couldn't sign out." },
  });
}

export function useDeleteMe() {
  const qc = useQueryClient();
  return useMutation<void, unknown, void>({
    mutationFn: deleteMe,
    onSuccess: () => {
      qc.clear();
    },
    meta: { errorToast: "Couldn't delete your account." },
  });
}
