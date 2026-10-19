/**
 * Browser-side init for the Sentry SDK pointed at our self-hosted GlitchTip.
 * The SDK speaks the Sentry wire protocol; GlitchTip implements it on the
 * server side, so the same `@sentry/nextjs` package drives both error capture
 * and distributed tracing into GlitchTip without modification.
 *
 * What's enabled:
 *   - Error capture (default integrations).
 *   - Browser tracing — auto-instruments fetch and history navigation, and
 *     adds `sentry-trace` + `baggage` headers to outbound fetches so the
 *     Django presentation-api (which has its own SDK pointed at the same
 *     GlitchTip) can continue the trace.
 * What's disabled:
 *   - Session replay (~40 KB extra bundle, GlitchTip support is partial,
 *     and the captured DOM would include course / assignment text which is
 *     student educational data).
 *   - `sendDefaultPii` — no IP, no cookies, no auth headers in payloads.
 *   - Profiling — not supported by GlitchTip.
 */

import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "development",
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),

  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1.0,

  replaysSessionSampleRate: 0,
  replaysOnErrorSampleRate: 0,

  sendDefaultPii: false,

  beforeSend(event) {
    // Strip identifying fields that the SDK may auto-attach.
    if (event.user?.email) {
      delete event.user.email;
    }
    if (event.user?.ip_address) {
      delete event.user.ip_address;
    }
    if (event.request?.headers) {
      const headers = event.request.headers as Record<string, string>;
      delete headers["Cookie"];
      delete headers["cookie"];
      delete headers["Authorization"];
      delete headers["authorization"];
    }
    return event;
  },
});
