"use client";

import { useEffect, useState } from "react";

import { useAuthStore } from "../store";
import { useMoodleLogin } from "./useMoodleLogin";

type Props = {
  open: boolean;
  onSuccessAction: () => void;
  onCloseAction: () => void;
};

export function MoodleLoginModal({ open, onSuccessAction, onCloseAction }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const { submitting, error, submit } = useMoodleLogin();
  const status = useAuthStore((s) => s.status);

  useEffect(() => {
    if (!open) return;
    if (status !== "authed") return;
    onSuccessAction();
  }, [open, status, onSuccessAction]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseAction();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCloseAction]);

  if (!open) return null;

  return (
    <div className="auth-modal-backdrop" role="dialog" aria-modal aria-label="Sign in with Moodle">
      <div className="auth-modal">
        <header className="auth-modal-header">
          <div>
            <h2>Sign in with Moodle</h2>
            <p className="auth-modal-status">
              We&apos;ll exchange your username and password for a one-time
              token. Your password is never stored.
            </p>
          </div>
          <button
            type="button"
            className="auth-modal-close"
            onClick={onCloseAction}
            disabled={submitting}
          >
            Close
          </button>
        </header>

        <form
          className="auth-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!username || !password || submitting) return;
            void submit({ username, password });
          }}
        >
          <label className="field">
            <span>Moodle username</span>
            <input
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              required
              autoFocus
            />
          </label>

          <label className="field">
            <span>Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
            />
          </label>

          {error ? (
            <p className="auth-form-error" role="alert">
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            className="btn-primary btn-large"
            disabled={submitting || !username || !password}
          >
            {submitting ? "Signing in…" : "Continue with Moodle"}
          </button>
        </form>
      </div>
    </div>
  );
}
