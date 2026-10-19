import { withSentryConfig } from "@sentry/nextjs";
import type { NextConfig } from "next";
import path from "path";
import { fileURLToPath } from "url";

const appDir = path.dirname(fileURLToPath(import.meta.url));

// Default to the docker-internal hostname so the production image works
// out of the box when built without env. Hybrid dev (Next on host) must
// pass HEADLESS_PUBLIC_URL=http://localhost:8000 explicitly via the
// `npm run dev` invocation, which the docs already document.
const headlessUrl = process.env.HEADLESS_PUBLIC_URL ?? "http://headless:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  /* Keep Turbopack rooted to this app when the editor workspace folder is the parent repo */
  turbopack: {
    root: appDir,
  },
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "lh3.googleusercontent.com",
        pathname: "/**",
      },
    ],
  },
  async rewrites() {
    return [
      {
        source: "/headless-ws/:id",
        destination: `${headlessUrl}/auth/teams/:id/ws`,
      },
      {
        source: "/headless/:path*",
        destination: `${headlessUrl}/auth/teams/:path*`,
      },
    ];
  },
};

/**
 * Sentry → GlitchTip wrapping. The SDK uploads source maps via the Sentry
 * wire protocol, which GlitchTip implements at the same endpoints, so we
 * just point `url` at our self-hosted instance.
 *
 * Source-map upload only runs when both SENTRY_AUTH_TOKEN and the org/project
 * envs are set — in dev that's typically off, so the wrapper is a no-op.
 */
export default withSentryConfig(nextConfig, {
  org: process.env.SENTRY_ORG ?? "endpoint-local",
  project: process.env.SENTRY_PROJECT ?? "endpoint-frontend",
  sentryUrl: process.env.SENTRY_URL ?? "http://localhost:8011/",
  authToken: process.env.SENTRY_AUTH_TOKEN,
  silent: !process.env.CI,
  widenClientFileUpload: true,
  sourcemaps: {
    // After upload, drop the source-map files so they aren't served publicly.
    filesToDeleteAfterUpload: [".next/**/*.map"],
  },
  // Skip the tunnel route — it relays events through Next.js to Sentry to
  // bypass ad-blockers, but our DSN already points at an internal GlitchTip
  // host that ad-blockers don't know about.
  tunnelRoute: undefined,
});
