import type { UserListItem } from '@/api/queries';
import type { ChipTone } from '@/components/ui';

export type UserHealth = 'ok' | 'warn' | 'err' | 'paused';

export interface PlatformState {
  platform: string;
  connected: boolean;
  expired: boolean;
  last_successful_sync_at: string | null;
  last_refreshed_at: string | null;
}

/** Extract per-platform entries from the loose `platforms` dict in UserListItem. */
export function extractPlatforms(u: UserListItem): PlatformState[] {
  const raw = u.platforms;
  if (raw === null || typeof raw !== 'object') return [];
  const out: PlatformState[] = [];
  for (const [platform, v] of Object.entries(raw as Record<string, unknown>)) {
    if (v === null || typeof v !== 'object') continue;
    const rec = v as Record<string, unknown>;
    out.push({
      platform,
      connected: rec['connected'] === true,
      expired: rec['expired'] === true,
      last_successful_sync_at:
        typeof rec['last_successful_sync_at'] === 'string'
          ? rec['last_successful_sync_at']
          : null,
      last_refreshed_at:
        typeof rec['last_refreshed_at'] === 'string' ? rec['last_refreshed_at'] : null,
    });
  }
  return out;
}

/** Classify a user's overall health based on platform + paused state. */
export function userHealth(u: UserListItem): UserHealth {
  if (u.any_paused) return 'paused';
  if (u.any_expired) return 'err';
  const plats = extractPlatforms(u);
  if (plats.length === 0) return 'warn';
  const anyDisconnected = plats.some((p) => !p.connected);
  return anyDisconnected ? 'warn' : 'ok';
}

export function healthTone(h: UserHealth): ChipTone {
  if (h === 'ok') return 'ok';
  if (h === 'warn') return 'warn';
  if (h === 'err') return 'err';
  return 'idle';
}

export function healthLabel(h: UserHealth): string {
  if (h === 'ok') return 'healthy';
  if (h === 'warn') return 'warning';
  if (h === 'err') return 'failing';
  return 'paused';
}

export function matchesUserQuery(u: UserListItem, q: string): boolean {
  if (q.length === 0) return true;
  const needle = q.toLowerCase();
  return (
    u.email.toLowerCase().includes(needle) ||
    (u.display_name?.toLowerCase().includes(needle) ?? false)
  );
}

export function initialsFor(u: UserListItem): string {
  const src = u.display_name ?? u.email;
  return src
    .split(/[\s.@_-]+/)
    .filter((s) => s.length > 0)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase() ?? '')
    .join('');
}
