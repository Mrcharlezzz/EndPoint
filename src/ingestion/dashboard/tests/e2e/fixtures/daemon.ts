import { WebSocket } from 'node:http';
import type { APIResponse, Page } from '@playwright/test';

export const ADMIN_EMAIL = 'e2e-admin@test.local';
export const ADMIN_PASSWORD = 'e2e-password-DO-NOT-USE-IN-PROD';

export interface SeededUser {
  email: string;
  displayName: string;
  platform: 'teams' | 'moodle' | 'telegram';
  isExpired: boolean;
  isPaused: boolean;
}

/** Mirrors the order of the `USERS` list in `scripts/seed_test_errors.py`.
 *  Update both together when the seed changes. */
const SEEDED_USERS: readonly SeededUser[] = [
  { email: 'sara@e2e.test',  displayName: 'Sara Fitzgerald',  platform: 'teams',    isExpired: false, isPaused: false },
  { email: 'sara@e2e.test',  displayName: 'Sara Fitzgerald',  platform: 'moodle',   isExpired: false, isPaused: false },
  { email: 'pavel@e2e.test', displayName: 'Pavel Novak',      platform: 'teams',    isExpired: true,  isPaused: false },
  { email: 'pavel@e2e.test', displayName: 'Pavel Novak',      platform: 'moodle',   isExpired: false, isPaused: false },
  { email: 'kenji@e2e.test', displayName: 'Kenji Yamamoto',   platform: 'telegram', isExpired: false, isPaused: true  },
  { email: 'nour@e2e.test',  displayName: 'Nour Haddad',      platform: 'moodle',   isExpired: true,  isPaused: false },
  { email: 'olek@e2e.test',  displayName: 'Olek Kowalski',    platform: 'teams',    isExpired: false, isPaused: false },
] as const;

export function seededUser(idx: number): SeededUser {
  const user = SEEDED_USERS[idx];
  if (!user) {
    throw new Error(`seededUser(${idx}): only ${SEEDED_USERS.length} seeded rows available`);
  }
  return user;
}

export function daemonUrl(): string {
  const url = process.env.VITE_DAEMON_URL;
  if (!url) {
    throw new Error('VITE_DAEMON_URL is unset — globalSetup must populate it before tests run');
  }
  return url;
}

export interface ApiCallInit {
  method?: string;
  data?: unknown;
  headers?: Record<string, string>;
}

/** Hit the daemon directly (bypassing the dashboard UI). Carries whatever
 *  cookies the page's browser context already holds — i.e. the session
 *  established by `login(page)`. */
export async function apiCall(
  page: Page,
  path: string,
  init: ApiCallInit = {},
): Promise<APIResponse> {
  return page.request.fetch(`${daemonUrl()}${path}`, {
    method: init.method ?? 'GET',
    ...(init.data !== undefined ? { data: init.data as object } : {}),
    ...(init.headers ? { headers: init.headers } : {}),
  });
}

/** Drive the real login form, returning once the post-login redirect lands. */
export async function login(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(/admin password/i).fill(ADMIN_PASSWORD);
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith('/login'));
}

/** Open a WebSocket directly to the daemon, subscribe to ``channel``, and
 *  resolve with the first event that satisfies ``predicate``. The test owns
 *  the socket lifecycle — the dashboard's in-app ``useChannel`` is unaffected. */
export function waitForWs<P>(
  _page: Page,
  channel: string,
  predicate: (payload: P) => boolean,
  timeoutMs = 5000,
): Promise<P> {
  const wsBase = daemonUrl().replace(/^http/, 'ws');
  const wsUrl = `${wsBase}${channel}`;
  return new Promise<P>((resolve, reject) => {
    const socket = new WebSocket(wsUrl);
    const timer = setTimeout(() => {
      socket.close();
      reject(new Error(`waitForWs(${channel}): no matching event within ${timeoutMs}ms`));
    }, timeoutMs);
    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({}));
    });
    socket.addEventListener('message', (event: MessageEvent) => {
      let payload: P;
      try {
        payload = JSON.parse(String(event.data)) as P;
      } catch {
        return;
      }
      if (predicate(payload)) {
        clearTimeout(timer);
        socket.close();
        resolve(payload);
      }
    });
    socket.addEventListener('error', () => {
      clearTimeout(timer);
      reject(new Error(`waitForWs(${channel}): socket error on ${wsUrl}`));
    });
  });
}
