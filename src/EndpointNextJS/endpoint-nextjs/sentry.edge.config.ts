/**
 * Edge runtime init for Sentry → GlitchTip. Used by Next.js when middleware
 * or route handlers run on the edge runtime; we don't currently use either,
 * but the SDK requires the file to exist if we wrap the build with
 * `withSentryConfig`.
 */

import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "development",
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),

  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1.0,

  sendDefaultPii: false,
});
