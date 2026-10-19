import { defineConfig, devices } from '@playwright/test';

/**
 * E2E harness for the ingestor dashboard.
 *
 * The lifecycle is owned by `tests/e2e/global-setup.ts`:
 *   1. spawn `scripts/e2e_dashboard_bootstrap.py --keep-alive` (real daemon
 *      + 4 platform stubs + seeded Postgres/SQLite);
 *   2. scrape DAEMON_URL from its stdout;
 *   3. build the dashboard with `VITE_DAEMON_URL` baked in;
 *   4. start `pnpm preview` on :4173.
 * `tests/e2e/global-teardown.ts` reverses that.
 *
 * There is intentionally no `webServer` block — Playwright runs `webServer`
 * before `globalSetup`, which would mean our build bakes in an empty
 * daemon URL. Owning both phases in globalSetup keeps the ordering right.
 */
export default defineConfig({
  testDir: './tests/e2e',
  globalSetup: './tests/e2e/global-setup.ts',
  globalTeardown: './tests/e2e/global-teardown.ts',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI !== undefined ? 2 : 0,
  reporter: process.env.CI !== undefined ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
