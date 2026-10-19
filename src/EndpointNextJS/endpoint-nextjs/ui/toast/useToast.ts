import { useToastStore } from "./store";
import type { ToastVariant } from "./types";

const DEFAULT_DURATION_MS = 4000;

function push(variant: ToastVariant, message: string, durationMs?: number | null): string {
  const fallback = variant === "loading" ? null : DEFAULT_DURATION_MS;
  return useToastStore.getState().push({
    variant,
    message,
    durationMs: durationMs === undefined ? fallback : durationMs,
  });
}

export const toast = {
  info(message: string, durationMs?: number) {
    return push("info", message, durationMs);
  },
  success(message: string, durationMs?: number) {
    return push("success", message, durationMs);
  },
  error(message: string, durationMs?: number) {
    return push("error", message, durationMs);
  },
  loading(message: string) {
    return push("loading", message, null);
  },
  dismiss(id: string) {
    useToastStore.getState().dismiss(id);
  },
  clear() {
    useToastStore.getState().clear();
  },
};

export function useToast() {
  return toast;
}
