"use client";

import { useToastStore } from "./store";
import "./toast.css";

export function ToastViewport() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  return (
    <ol className="toast-viewport" aria-live="polite" aria-relevant="additions">
      {toasts.map((t) => (
        <li
          key={t.id}
          className={`toast toast--${t.variant}`}
          role={t.variant === "error" ? "alert" : "status"}
        >
          <span className="toast-message">{t.message}</span>
          <button
            type="button"
            className="toast-dismiss"
            aria-label="Dismiss notification"
            onClick={() => dismiss(t.id)}
          >
            <span aria-hidden>×</span>
          </button>
        </li>
      ))}
    </ol>
  );
}
