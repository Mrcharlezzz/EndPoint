"use client";

import { getPostHog } from "./client";

/**
 * Typed event registry. Adding a new event = adding a key here. The
 * `track()` function then enforces the right property shape at the
 * call site so we don't drift schema-by-typo.
 *
 * Naming convention: `<noun>.<verb_past_tense>` (snake_case verbs) so
 * funnels read naturally in PostHog's UI.
 */
export type EventMap = {
  "onboarding.step_complete": { step: string };
  "onboarding.step_skipped": { step: string };
  "onboarding.finished": Record<string, never>;

  "connection.linked": { platform: "moodle" | "teams" | "telegram" };
  "connection.disconnected": { platform: "moodle" | "teams" };

  "dashboard.viewed": { dashboard: "assignments" | "announcements" };

  "item.marked_read": { count: number };
  "read_history.cleared": Record<string, never>;

  "preference.changed": { key: string; value: string | number | boolean };

  "search.performed": { scope: "current" | "all"; result_count: number };
};

export function track<K extends keyof EventMap>(
  event: K,
  properties: EventMap[K],
): void {
  const ph = getPostHog();
  if (!ph) return;
  ph.capture(event, properties);
}
