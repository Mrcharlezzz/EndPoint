import { create } from 'zustand';

export type ToastTone = 'info' | 'success' | 'warn' | 'error';

export interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  message?: string | undefined;
  /** Milliseconds before auto-dismiss. 0 = sticky. */
  ttl: number;
}

interface ToastState {
  toasts: readonly Toast[];
  push: (t: Omit<Toast, 'id' | 'ttl'> & { ttl?: number }) => number;
  dismiss: (id: number) => void;
  clear: () => void;
}

let nextId = 1;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: ({ tone, title, message, ttl }) => {
    const id = nextId;
    nextId += 1;
    const finalTtl = ttl ?? (tone === 'error' ? 4000 : 2000);
    set((s) => ({ toasts: [...s.toasts, { id, tone, title, message, ttl: finalTtl }] }));
    if (finalTtl > 0) {
      window.setTimeout(() => {
        set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
      }, finalTtl);
    }
    return id;
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  clear: () => set({ toasts: [] }),
}));

/** Imperative access — safe to call from outside React (e.g. QueryCache handlers). */
export const toast = {
  info: (title: string, message?: string) => useToastStore.getState().push({ tone: 'info', title, message }),
  success: (title: string, message?: string) =>
    useToastStore.getState().push({ tone: 'success', title, message }),
  warn: (title: string, message?: string) =>
    useToastStore.getState().push({ tone: 'warn', title, message }),
  error: (title: string, message?: string) =>
    useToastStore.getState().push({ tone: 'error', title, message }),
  dismiss: (id: number) => useToastStore.getState().dismiss(id),
};
