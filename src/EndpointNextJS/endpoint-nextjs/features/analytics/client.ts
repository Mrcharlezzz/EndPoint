"use client";

import type { PostHog } from "posthog-js";

/**
 * PostHog client loader. Imports posthog-js dynamically so the ~50 KB
 * gzipped library doesn't land in the initial bundle — auth pages can
 * render before analytics is even on the wire.
 *
 * Returns the singleton instance, or null if no API key is configured
 * (e.g. local dev without a PostHog server, or a fresh checkout).
 */

let instance: PostHog | null = null;
let loadPromise: Promise<PostHog | null> | null = null;

const POSTHOG_KEY = process.env.NEXT_PUBLIC_POSTHOG_KEY;
const POSTHOG_HOST = process.env.NEXT_PUBLIC_POSTHOG_HOST ?? "http://localhost:8010";

export async function loadPostHog(): Promise<PostHog | null> {
  if (typeof window === "undefined") return null;
  if (!POSTHOG_KEY) return null;
  if (instance) return instance;
  if (loadPromise) return loadPromise;

  loadPromise = (async () => {
    const { default: posthog } = await import("posthog-js");
    posthog.init(POSTHOG_KEY, {
      api_host: POSTHOG_HOST,
      autocapture: false,
      capture_pageview: false,
      capture_pageleave: true,
      // Don't create anonymous profiles — only count identified users.
      // This keeps the user list aligned with real student accounts.
      person_profiles: "identified_only",
      disable_session_recording: true,
      disable_surveys: true,
      disable_external_dependency_loading: true,
    });
    instance = posthog;
    return posthog;
  })();
  return loadPromise;
}

export function getPostHog(): PostHog | null {
  return instance;
}
