"use client";

import { useEffect, useRef, useState } from "react";

import type { TeamsHeadlessSession } from "./useTeamsHeadlessSession";

type Props = {
  session: TeamsHeadlessSession;
};

const SPECIAL_KEYS = new Set([
  "Backspace",
  "Enter",
  "Tab",
  "Escape",
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Delete",
]);

const WARM_CACHE_HINT_DELAY_MS = 4_000;

/**
 * Renders the streamed JPEG frames from the headless service into a
 * canvas, and forwards pointer/keyboard input back through the session
 * hook. The hidden input field exists so mobile keyboards have something
 * to focus when the user taps inside the canvas.
 *
 * Before the first frame arrives, shows a skeleton placeholder with a
 * shimmer animation. After 4s with still no frame, surfaces a microcopy
 * line that the first launch is slow.
 */
export function TeamsLoginCanvas({ session }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const bmp = session.frame;
    if (!canvas || !bmp) return;
    if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
      canvas.width = bmp.width;
      canvas.height = bmp.height;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(bmp, 0, 0);
  }, [session.frame]);

  const showSkeleton = session.frame === null;

  return (
    <div className="teams-login-canvas">
      <canvas
        ref={canvasRef}
        width={460}
        height={640}
        onPointerDown={(e) => {
          e.preventDefault();
          const canvas = canvasRef.current;
          if (!canvas) return;
          const rect = canvas.getBoundingClientRect();
          const x = Math.round(((e.clientX - rect.left) * canvas.width) / rect.width);
          const y = Math.round(((e.clientY - rect.top) * canvas.height) / rect.height);
          session.send({ type: "click", x, y });
          inputRef.current?.focus({ preventScroll: true });
        }}
        aria-label="Microsoft sign-in canvas"
      />
      <input
        ref={inputRef}
        type="text"
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        inputMode="text"
        aria-hidden
        tabIndex={-1}
        onInput={(e) => {
          const target = e.currentTarget;
          const text = target.value;
          if (text) session.send({ type: "type", text });
          target.value = "";
        }}
        onKeyDown={(e) => {
          if (SPECIAL_KEYS.has(e.key)) {
            e.preventDefault();
            session.send({ type: "press", key: e.key });
          }
        }}
      />

      {showSkeleton ? (
        <TeamsLoginSkeleton />
      ) : null}

      {session.status === "collecting" ? (
        <div className="teams-login-overlay" role="status" aria-live="polite">
          <div className="teams-login-spinner" />
          <p>Capturing your tokens — hang tight.</p>
        </div>
      ) : null}
    </div>
  );
}

function TeamsLoginSkeleton() {
  const [warmHintShown, setWarmHintShown] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => setWarmHintShown(true), WARM_CACHE_HINT_DELAY_MS);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <div className="teams-login-skeleton" aria-hidden>
      <div className="teams-login-skeleton-bar teams-login-skeleton-bar--logo" />
      <div className="teams-login-skeleton-bar teams-login-skeleton-bar--title" />
      <div className="teams-login-skeleton-bar teams-login-skeleton-bar--field" />
      <div className="teams-login-skeleton-bar teams-login-skeleton-bar--button" />
      {warmHintShown ? (
        <p className="teams-login-skeleton-hint">
          First launch is slow — next time it&apos;s instant.
        </p>
      ) : null}
    </div>
  );
}
