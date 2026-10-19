export type ToastVariant = "info" | "success" | "error" | "loading";

export type Toast = {
  id: string;
  variant: ToastVariant;
  message: string;
  durationMs: number | null;
};
