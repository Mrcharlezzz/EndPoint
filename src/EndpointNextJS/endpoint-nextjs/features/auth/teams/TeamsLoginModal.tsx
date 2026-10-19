"use client";

import { useEffect, useState } from "react";

import { finalizeTeamsSession } from "./finalize";
import { TeamsLoginCanvas } from "./TeamsLoginCanvas";
import {
  useTeamsHeadlessSession,
  type TeamsPhase,
  type TeamsHeadlessSession,
} from "./useTeamsHeadlessSession";

type Props = {
  open: boolean;
  onSuccessAction: () => void;
  onCloseAction: () => void;
};

const PHASE_COPY: Record<TeamsPhase, { title: string; sub?: string }> = {
  boot: { title: "Spinning up a private browser…", sub: "First time can take ~10s." },
  loading: { title: "Loading Microsoft sign-in…" },
  signin: { title: "Sign in below. We never see your password." },
  mfa_wait: {
    title: "Still waiting on Microsoft.",
    sub: "Two-factor codes can take a minute.",
  },
  stalled: {
    title: "This is taking longer than usual.",
    sub: "If your authenticator app didn't get a prompt, retry below.",
  },
  collecting: { title: "Verifying your account…" },
  finalizing: { title: "Saving your connection…" },
  done: { title: "Connected. Continuing…" },
  error: { title: "Sign-in failed." },
};

const CHIP_PHASES: ReadonlyArray<{
  id: "browse" | "signin" | "verify";
  label: string;
  matches: ReadonlyArray<TeamsPhase>;
}> = [
  { id: "browse", label: "Open browser", matches: ["boot", "loading"] },
  { id: "signin", label: "Sign in", matches: ["signin", "mfa_wait", "stalled"] },
  { id: "verify", label: "Verify", matches: ["collecting", "finalizing", "done"] },
];

function chipState(
  chipMatches: ReadonlyArray<TeamsPhase>,
  current: TeamsPhase,
  allChipMatches: ReadonlyArray<ReadonlyArray<TeamsPhase>>,
  chipIndex: number,
): "pending" | "active" | "done" {
  if (chipMatches.includes(current)) return "active";
  // If we're past this chip's range, mark as done.
  for (let i = chipIndex + 1; i < allChipMatches.length; i++) {
    if (allChipMatches[i]!.includes(current)) return "done";
  }
  // If error happened, leave subsequent chips as pending — don't claim done.
  return "pending";
}

/**
 * Maps the headless `error` string from session.py to a stable category
 * so we can show specific microcopy + retry actions per failure mode.
 */
function categorizeError(err: string): {
  title: string;
  body: string;
} {
  const lc = err.toLowerCase();
  if (lc.includes("timeout") || lc.includes("timed out")) {
    return {
      title: "We didn't see a successful sign-in.",
      body: "Microsoft didn't return tokens in time. Try again — most retries succeed.",
    };
  }
  if (lc.includes("refresh_token") || lc.includes("interactionrequired")) {
    return {
      title: "Microsoft accepted your sign-in but didn't issue a refresh token.",
      body: "This usually means your Microsoft tenant blocks third-party apps. Contact your IT admin or try a different account.",
    };
  }
  if (lc.includes("graph") || lc.includes("/me")) {
    return {
      title: "Signed in but couldn't reach Microsoft Graph.",
      body: "The account is valid but Graph rejected the token. Retry — this is usually transient.",
    };
  }
  return {
    title: "Sign-in failed.",
    body: err,
  };
}

/**
 * Modal wrapper around the Teams headless canvas. Owns the start-on-open
 * lifecycle and triggers finalize when the headless reaches CAPTURED.
 * The wizard handles navigation; this component just reports success.
 */
export function TeamsLoginModal({ open, onSuccessAction, onCloseAction }: Props) {
  const session = useTeamsHeadlessSession();

  // Auto-start when the modal opens for the first time.
  useEffect(() => {
    if (open && session.status === "idle") {
      void session.start();
    }
  }, [open, session]);

  // Trigger finalize once headless reports CAPTURED. Surfaces the error
  // back into the session store so the modal can render it.
  useEffect(() => {
    if (!open) return;
    if (session.status !== "captured") return;
    if (session.finalizing) return;
    if (session.finalizeError) return; // Already failed; wait for explicit retry.
    if (!session.sessionId) return;

    session.setFinalizing(true);
    void finalizeTeamsSession(session.sessionId)
      .then(() => onSuccessAction())
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : "Couldn't finish sign-in.";
        session.setFinalizeError(message);
      })
      .finally(() => {
        session.setFinalizing(false);
      });
  }, [
    open,
    session.status,
    session.sessionId,
    session.finalizing,
    session.finalizeError,
    session,
    onSuccessAction,
  ]);

  // Esc closes — but only if we're not mid-finalize (don't lose progress).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (session.finalizing) return;
      void session.stop().then(onCloseAction);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, session, onCloseAction]);

  const handleClose = () => {
    if (session.finalizing) {
      const ok = window.confirm(
        "Cancel sign-in? Your tokens are being saved — closing now may leave it half-done.",
      );
      if (!ok) return;
    }
    void session.stop().then(onCloseAction);
  };

  const handleRetry = async () => {
    session.setFinalizeError(null);
    // For headless errors, do a full restart; for finalize errors, just
    // re-trigger the finalize POST.
    if (session.error || session.status === "failed" || session.status === "closed") {
      await session.stop();
      await session.start();
      return;
    }
    if (session.finalizeError && session.sessionId) {
      session.setFinalizing(true);
      void finalizeTeamsSession(session.sessionId)
        .then(() => onSuccessAction())
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : "Couldn't finish sign-in.";
          session.setFinalizeError(message);
        })
        .finally(() => {
          session.setFinalizing(false);
        });
    }
  };

  if (!open) return null;

  const copy = PHASE_COPY[session.phase];
  const chipMatches = CHIP_PHASES.map((c) => c.matches);

  return (
    <div
      className="auth-modal-backdrop"
      role="dialog"
      aria-modal
      aria-label="Sign in with Microsoft"
    >
      <div className="auth-modal teams-login-modal">
        <header className="auth-modal-header">
          <div>
            <h2>Sign in with Microsoft</h2>
            <p className="auth-modal-status">{copy.title}</p>
            {copy.sub ? <p className="teams-login-substatus">{copy.sub}</p> : null}
          </div>
          <button
            type="button"
            className="auth-modal-close"
            onClick={handleClose}
            disabled={session.finalizing}
          >
            Close
          </button>
        </header>

        <div className="teams-login-chips" aria-hidden>
          {CHIP_PHASES.map((chip, idx) => {
            const state = chipState(chip.matches, session.phase, chipMatches, idx);
            return (
              <span key={chip.id} className="teams-login-chip" data-state={state}>
                {chip.label}
              </span>
            );
          })}
        </div>

        <TeamsLoginCanvas session={session} />

        <ErrorPanel session={session} onRetry={handleRetry} />
      </div>
    </div>
  );
}

function ErrorPanel({
  session,
  onRetry,
}: {
  session: TeamsHeadlessSession;
  onRetry: () => void | Promise<void>;
}) {
  const [retrying, setRetrying] = useState(false);
  const headlessErr = session.error;
  const finalizeErr = session.finalizeError;
  if (!headlessErr && !finalizeErr) return null;

  // Headless errors have richer categorization; finalize errors come back
  // from the presentation-api as plain strings.
  const detail = headlessErr
    ? categorizeError(headlessErr)
    : { title: "Couldn't save your connection.", body: finalizeErr ?? "" };

  const handleClick = async () => {
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="teams-login-error" role="alert">
      <div className="teams-login-error-title">{detail.title}</div>
      <p className="teams-login-error-body">{detail.body}</p>
      <button
        type="button"
        className="btn-primary btn-sm"
        onClick={handleClick}
        disabled={retrying}
      >
        {retrying ? "Retrying…" : "Retry"}
      </button>
    </div>
  );
}
