import { create } from "zustand";

import type { AuthUser } from "@/domain/auth";

export type AuthStatus = "idle" | "authenticating" | "authed" | "error";

type AuthState = {
  user: AuthUser | null;
  status: AuthStatus;
  error: string | null;
  setUser: (user: AuthUser | null) => void;
  setStatus: (status: AuthStatus, error?: string | null) => void;
  reset: () => void;
};

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: "idle",
  error: null,
  setUser: (user) =>
    set(() => ({
      user,
      status: user ? "authed" : "idle",
      error: null,
    })),
  setStatus: (status, error = null) => set(() => ({ status, error })),
  reset: () => set(() => ({ user: null, status: "idle", error: null })),
}));
