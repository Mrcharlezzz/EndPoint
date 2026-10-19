"use client";

import { useEffect, useState, type ReactNode } from "react";

type Status = "idle" | "connecting" | "connected";

type Props = {
  platform: "moodle" | "teams";
  iconLetter: string;
  title: string;
  blurb: string;
  perms: string[];
  /** Modal opens when status === "connecting". Children read this to drive auth. */
  renderModalAction: (args: {
    open: boolean;
    onSuccessAction: () => void;
    onCloseAction: () => void;
  }) => ReactNode;
  onContinueAction: () => void;
  onBackAction: () => void;
};

const STATUS_BLURB: Record<Status, string> = {
  idle: "",
  connecting: "Opening sign-in…",
  connected: "Continuing to next step…",
};

export function ConnectStep({
  platform,
  iconLetter,
  title,
  blurb,
  perms,
  renderModalAction,
  onContinueAction,
  onBackAction,
}: Props) {
  const [status, setStatus] = useState<Status>("idle");

  // When the modal reports success, briefly show the success badge then advance.
  useEffect(() => {
    if (status !== "connected") return;
    const id = window.setTimeout(() => onContinueAction(), 900);
    return () => window.clearTimeout(id);
  }, [status, onContinueAction]);

  const modalOpen = status === "connecting";

  return (
    <div className="onboarding-card onboarding-card--narrow">
      <div className="connect-platform-icon" data-platform={platform} aria-hidden>
        {iconLetter}
      </div>

      <h1 className="onboarding-title">{title}</h1>
      <p className="onboarding-blurb">{blurb}</p>

      <div className="connect-perms">
        <h3 className="perms-title">What we&apos;ll access</h3>
        <ul>
          {perms.map((p) => (
            <li key={p}>
              <span className="check" aria-hidden>
                ✓
              </span>
              <span>{p}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="connect-state" data-state={status}>
        {status === "idle" ? (
          <button
            type="button"
            className="btn-primary btn-large"
            onClick={() => setStatus("connecting")}
          >
            Connect {platform === "moodle" ? "Moodle" : "Microsoft"}
          </button>
        ) : null}

        {status === "connecting" ? (
          <div className="connect-spinner-row" role="status">
            <span className="connect-spinner" aria-hidden />
            <span>{STATUS_BLURB.connecting}</span>
          </div>
        ) : null}

        {status === "connected" ? (
          <div className="connect-success" role="status">
            <span className="success-check" aria-hidden>
              ✓
            </span>
            <div>
              <div className="success-title">Connected</div>
              <div className="success-sub">{STATUS_BLURB.connected}</div>
            </div>
          </div>
        ) : null}
      </div>

      <div className="onboarding-actions">
        <button
          type="button"
          className="btn-ghost"
          onClick={onBackAction}
          disabled={status !== "idle"}
        >
          ← Back
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={onContinueAction}
          disabled={status !== "idle"}
        >
          Skip for now
        </button>
      </div>

      {renderModalAction({
        open: modalOpen,
        onSuccessAction: () => setStatus("connected"),
        onCloseAction: () => setStatus("idle"),
      })}
    </div>
  );
}
