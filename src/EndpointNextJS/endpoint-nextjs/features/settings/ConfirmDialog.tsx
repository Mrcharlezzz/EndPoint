"use client";

import { useEffect } from "react";

type Props = {
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  confirmDisabled?: boolean;
  onConfirmAction: () => void;
  onCancelAction: () => void;
};

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger,
  confirmDisabled,
  onConfirmAction,
  onCancelAction,
}: Props) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancelAction();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onCancelAction]);

  return (
    <div
      className="confirm-overlay"
      role="dialog"
      aria-modal
      aria-labelledby="confirm-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancelAction();
      }}
    >
      <div className="confirm-dialog">
        <h3 id="confirm-title" className="confirm-title">
          {title}
        </h3>
        <p className="confirm-body">{body}</p>
        <div className="confirm-actions">
          <button type="button" className="btn-secondary" onClick={onCancelAction}>
            Cancel
          </button>
          <button
            type="button"
            className={danger ? "btn-danger" : "btn-primary"}
            onClick={onConfirmAction}
            disabled={confirmDisabled}
            autoFocus
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
