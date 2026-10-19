"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { create } from "zustand";

import { log } from "@/lib/logger";

/**
 * Status mirrors the headless service contract — see
 * src/ingestion/ingestion/headless/api.py and the handoff doc Section 5.
 */
export type HeadlessStatus =
  | "idle"
  | "starting"
  | "waiting"
  | "collecting"
  | "captured"
  | "failed"
  | "closed";

/**
 * UX-facing phase derived from `status` + elapsed time + finalize state.
 * Wider granularity than the headless service exposes natively, so the UI
 * can show distinct microcopy / chips for each.
 */
export type TeamsPhase =
  | "boot" // status=starting; chromium booting
  | "loading" // status=waiting AND no frame yet; navigating to login.microsoftonline.com
  | "signin" // status=waiting AND frame; user typing credentials
  | "mfa_wait" // status=waiting AND frame AND >30s since first frame
  | "stalled" // status=waiting AND frame AND >90s since first frame; show recovery
  | "collecting" // status=collecting
  | "finalizing" // status=captured; backend finalize in flight
  | "done" // finalize succeeded
  | "error"; // headless OR finalize failed

type TeamsHeadlessState = {
  sessionId: string | null;
  status: HeadlessStatus;
  error: string | null;
  /** Frontend-only: error from the finalize POST (separate from headless errors). */
  finalizeError: string | null;
  /** True while the finalize POST is in flight. */
  finalizing: boolean;
  /** Latest decoded JPEG frame; consumed by <canvas> via ref. */
  frame: ImageBitmap | null;
  /** ms since epoch when the session was created. Drives elapsed-time phases. */
  startedAt: number | null;
  /** ms since epoch when the first frame arrived. */
  firstFrameAt: number | null;
  setSession: (id: string | null) => void;
  setStatus: (s: HeadlessStatus, error?: string | null) => void;
  setFrame: (bmp: ImageBitmap | null) => void;
  setFinalizing: (v: boolean) => void;
  setFinalizeError: (msg: string | null) => void;
  reset: () => void;
};

const useTeamsHeadlessStore = create<TeamsHeadlessState>((set) => ({
  sessionId: null,
  status: "idle",
  error: null,
  finalizeError: null,
  finalizing: false,
  frame: null,
  startedAt: null,
  firstFrameAt: null,
  setSession: (sessionId) =>
    set(() => ({
      sessionId,
      startedAt: sessionId ? Date.now() : null,
    })),
  setStatus: (status, error = null) => set(() => ({ status, error })),
  setFrame: (frame) =>
    set((s) => ({
      frame,
      firstFrameAt: s.firstFrameAt ?? (frame ? Date.now() : null),
    })),
  setFinalizing: (finalizing) => set(() => ({ finalizing })),
  setFinalizeError: (finalizeError) => set(() => ({ finalizeError })),
  reset: () =>
    set((s) => {
      if (s.frame) s.frame.close();
      return {
        sessionId: null,
        status: "idle",
        error: null,
        finalizeError: null,
        finalizing: false,
        frame: null,
        startedAt: null,
        firstFrameAt: null,
      };
    }),
}));

type WireEvent =
  | { type: "click"; x: number; y: number }
  | { type: "type"; text: string }
  | { type: "press"; key: string };

export type TeamsHeadlessSession = {
  sessionId: string | null;
  status: HeadlessStatus;
  /** Headless-side error (chromium / network / token mint failure). */
  error: string | null;
  /** Backend finalize error (Graph validation, DB write, etc.). */
  finalizeError: string | null;
  /** UX phase derived from status + elapsed time + finalize state. */
  phase: TeamsPhase;
  /** ms elapsed since the session started (for stall thresholds). */
  elapsedMs: number;
  /** ms elapsed since the first JPEG frame arrived (0 if none yet). */
  elapsedSinceFirstFrameMs: number;
  finalizing: boolean;
  frame: ImageBitmap | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  send: (event: WireEvent) => void;
  setFinalizing: (v: boolean) => void;
  setFinalizeError: (msg: string | null) => void;
};

/**
 * Drives the headless Teams login session: kicks off /headless/start, opens
 * the WebSocket for frame streaming + input, polls /headless/{id} for status
 * transitions, and exposes a small action surface for the UI.
 *
 * Frame decoding happens here so the canvas component just renders the
 * latest ImageBitmap from the store.
 */
export function useTeamsHeadlessSession(): TeamsHeadlessSession {
  const sessionId = useTeamsHeadlessStore((s) => s.sessionId);
  const status = useTeamsHeadlessStore((s) => s.status);
  const error = useTeamsHeadlessStore((s) => s.error);
  const finalizeError = useTeamsHeadlessStore((s) => s.finalizeError);
  const finalizing = useTeamsHeadlessStore((s) => s.finalizing);
  const frame = useTeamsHeadlessStore((s) => s.frame);
  const startedAt = useTeamsHeadlessStore((s) => s.startedAt);
  const firstFrameAt = useTeamsHeadlessStore((s) => s.firstFrameAt);

  const wsRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<number | null>(null);
  // Tick state every second so elapsed-time phases re-render. We don't
  // store the tick value — just use it as a re-render trigger.
  const tickRef = useRef<number | null>(null);
  const [, forceTick] = useTickEverySecond();

  const elapsedMs = startedAt ? Date.now() - startedAt : 0;
  const elapsedSinceFirstFrameMs = firstFrameAt ? Date.now() - firstFrameAt : 0;

  const phase = derivePhase({
    status,
    error,
    finalizeError,
    finalizing,
    hasFrame: frame !== null,
    elapsedSinceFirstFrameMs,
  });

  const closeSocket = useCallback(() => {
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    }
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (tickRef.current != null) {
      window.clearInterval(tickRef.current);
      tickRef.current = null;
    }
  }, []);

  const send = useCallback((event: WireEvent) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(event));
    }
  }, []);

  const stop = useCallback(async () => {
    const id = useTeamsHeadlessStore.getState().sessionId;
    closeSocket();
    if (id) {
      try {
        await fetch(`/headless/${id}`, { method: "DELETE" });
      } catch (err) {
        log.warn("teams.headless.stop.error", { message: String(err) });
      }
    }
    useTeamsHeadlessStore.getState().reset();
  }, [closeSocket]);

  const connectWS = useCallback((id: string) => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/headless-ws/${id}`);
    ws.binaryType = "blob";

    ws.onmessage = (e) => {
      if (!(e.data instanceof Blob)) return;
      createImageBitmap(e.data)
        .then((bmp) => {
          const prev = useTeamsHeadlessStore.getState().frame;
          if (prev) prev.close();
          useTeamsHeadlessStore.getState().setFrame(bmp);
        })
        .catch((err) => log.warn("teams.headless.frame.decode", { message: String(err) }));
    };

    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
    };

    ws.onerror = () => log.warn("teams.headless.ws.error", { type: "ws_error" });

    wsRef.current = ws;
  }, []);

  const startPolling = useCallback((id: string) => {
    const tick = async () => {
      try {
        const r = await fetch(`/headless/${id}`);
        if (!r.ok) return;
        const data = (await r.json()) as { status: HeadlessStatus; error?: string };
        const setStatus = useTeamsHeadlessStore.getState().setStatus;
        setStatus(data.status, data.error ?? null);
        if (
          data.status === "captured" ||
          data.status === "failed" ||
          data.status === "closed"
        ) {
          if (pollRef.current != null) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }
      } catch (err) {
        log.warn("teams.headless.poll.error", { message: String(err) });
      }
    };
    pollRef.current = window.setInterval(tick, 2000);
    void tick();
  }, []);

  const start = useCallback(async () => {
    const setStatus = useTeamsHeadlessStore.getState().setStatus;
    const setSession = useTeamsHeadlessStore.getState().setSession;
    setStatus("starting");
    try {
      const r = await fetch("/headless/start", { method: "POST" });
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        const message = (detail as { detail?: string }).detail ?? `Start failed: ${r.status}`;
        throw new Error(message);
      }
      const data = (await r.json()) as { session_id: string; status: HeadlessStatus };
      setSession(data.session_id);
      setStatus(data.status);
      connectWS(data.session_id);
      startPolling(data.session_id);
      // Force a re-render every second so elapsed-time phases tick over.
      tickRef.current = window.setInterval(forceTick, 1000);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to start session";
      log.error("teams.headless.start.error", err);
      setStatus("failed", message);
    }
  }, [connectWS, startPolling, forceTick]);

  useEffect(() => {
    return () => {
      closeSocket();
      const f = useTeamsHeadlessStore.getState().frame;
      if (f) f.close();
    };
  }, [closeSocket]);

  const setFinalizing = useTeamsHeadlessStore((s) => s.setFinalizing);
  const setFinalizeError = useTeamsHeadlessStore((s) => s.setFinalizeError);

  return {
    sessionId,
    status,
    error,
    finalizeError,
    phase,
    elapsedMs,
    elapsedSinceFirstFrameMs,
    finalizing,
    frame,
    start,
    stop,
    send,
    setFinalizing,
    setFinalizeError,
  };
}

/**
 * Maps the raw status + elapsed time to a UX phase. Pure function so the
 * test suite can hammer the boundaries without any React/zustand glue.
 */
export function derivePhase(args: {
  status: HeadlessStatus;
  error: string | null;
  finalizeError: string | null;
  finalizing: boolean;
  hasFrame: boolean;
  elapsedSinceFirstFrameMs: number;
}): TeamsPhase {
  const { status, error, finalizeError, finalizing, hasFrame, elapsedSinceFirstFrameMs } = args;
  if (error || finalizeError) return "error";
  if (status === "failed") return "error";
  if (status === "starting") return "boot";
  if (status === "waiting") {
    if (!hasFrame) return "loading";
    if (elapsedSinceFirstFrameMs > 90_000) return "stalled";
    if (elapsedSinceFirstFrameMs > 30_000) return "mfa_wait";
    return "signin";
  }
  if (status === "collecting") return "collecting";
  if (status === "captured") {
    return finalizing ? "finalizing" : "done";
  }
  return "boot";
}

/**
 * Lightweight 1Hz tick to re-render so the elapsed-time-derived phases
 * pick up new boundaries without polling. The interval itself is started
 * inside `start()` and torn down by `closeSocket`.
 */
function useTickEverySecond(): [number, () => void] {
  const [tick, setTick] = useState(0);
  return [tick, () => setTick((n) => n + 1)];
}
