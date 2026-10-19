/**
 * Next.js 16 calls `register()` once per server runtime at startup. We dispatch
 * to the matching Sentry config so the SDK is initialised against the correct
 * runtime APIs (Node vs edge). The browser config loads via `withSentryConfig`'s
 * client-side injection, not from here.
 */

import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }
  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}

export const onRequestError = Sentry.captureRequestError;
