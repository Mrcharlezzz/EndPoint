/**
 * Server runtime (Node) init for Sentry → GlitchTip. Mirrors the client init
 * with sampling and PII-scrubbing rules, minus the browser-only integrations.
 */

import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "development",
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),

  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1.0,

  sendDefaultPii: false,

  beforeSend(event) {
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
