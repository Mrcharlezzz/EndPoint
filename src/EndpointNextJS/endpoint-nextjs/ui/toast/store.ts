import { create } from "zustand";

import type { Toast } from "./types";

type StoredToast = Toast & { _timer?: ReturnType<typeof setTimeout> };

type State = {
  toasts: StoredToast[];
  push(input: Omit<Toast, "id">): string;
  dismiss(id: string): void;
  clear(): void;
};

function nextId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return Math.random().toString(36).slice(2);
}

export const useToastStore = create<State>((set, get) => ({
  toasts: [],
  push(input) {
    const id = nextId();
    const toast: StoredToast = { id, ...input };
    if (input.durationMs != null && typeof window !== "undefined") {
      toast._timer = setTimeout(() => get().dismiss(id), input.durationMs);
    }
    set((s) => ({ toasts: [...s.toasts, toast] }));
    return id;
  },
  dismiss(id) {
    set((s) => {
      const target = s.toasts.find((t) => t.id === id);
      if (target?._timer) clearTimeout(target._timer);
      return { toasts: s.toasts.filter((t) => t.id !== id) };
    });
  },
  clear() {
    set((s) => {
      s.toasts.forEach((t) => {
        if (t._timer) clearTimeout(t._timer);
      });
      return { toasts: [] };
    });
  },
}));
