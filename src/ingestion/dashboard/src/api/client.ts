import createClient from 'openapi-fetch';
import type { paths } from './schema';

const UNAUTHORIZED_EVENT = 'api:unauthorized';

export function dispatchUnauthorized(): void {
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
}

export function onUnauthorized(handler: () => void): () => void {
  const listener = (): void => handler();
  window.addEventListener(UNAUTHORIZED_EVENT, listener);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, listener);
}

export const api = createClient<paths>({
  baseUrl: '',
  credentials: 'include',
  // Daemon's admin auth requires X-Requested-With on POST/PUT/PATCH/DELETE
  // as CSRF defense-in-depth (see daemon/api/auth.py). GET is unaffected
  // but the header is harmless on reads, so set it globally and stop
  // thinking about it per-endpoint.
  headers: { 'X-Requested-With': 'XMLHttpRequest' },
});

api.use({
  onResponse({ response }) {
    if (response.status === 401) dispatchUnauthorized();
    return response;
  },
});

export type { paths } from './schema';
