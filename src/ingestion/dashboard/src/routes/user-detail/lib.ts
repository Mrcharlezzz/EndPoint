import type { UserDetail, UserListItem } from '@/api/queries';
import type { ChipTone } from '@/components/ui';

export type UserHealth = 'ok' | 'warn' | 'err' | 'paused';

export interface PlatformRow {
  platform: string;
  is_paused: boolean;
  is_expired: boolean;
  last_refreshed_at: string | null;
  expired_at: string | null;
}

export function platformsOf(detail: UserDetail): PlatformRow[] {
  const entries = Object.entries(detail.platforms ?? {});
  return entries
    .map(([platform, raw]) => {
      if (raw === null || typeof raw !== 'object') return null;
      const rec = raw as Record<string, unknown>;
      return {
        platform,
        is_paused: rec['is_paused'] === true,
        is_expired: rec['is_expired'] === true,
        last_refreshed_at:
          typeof rec['last_refreshed_at'] === 'string' ? rec['last_refreshed_at'] : null,
        expired_at: typeof rec['expired_at'] === 'string' ? rec['expired_at'] : null,
      };
    })
    .filter((r): r is PlatformRow => r !== null)
    .sort((a, b) => a.platform.localeCompare(b.platform));
}

export function detailHealth(detail: UserDetail): UserHealth {
  const plats = platformsOf(detail);
  if (plats.some((p) => p.is_paused)) return 'paused';
  if (plats.some((p) => p.is_expired)) return 'err';
  if (plats.length === 0) return 'warn';
  return 'ok';
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

export function initialsOf(detail: UserDetail | UserListItem): string {
  const src = detail.display_name ?? detail.email;
  return src
    .split(/[\s.@_-]+/)
    .filter((s) => s.length > 0)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase() ?? '')
    .join('');
}

export const TAB_IDS = [
  'overview',
  'credentials',
  'syncs',
  'contributions',
  'errors',
  'audit',
] as const;
export type TabId = (typeof TAB_IDS)[number];

export const TAB_LABELS: Record<TabId, string> = {
  overview: 'Overview',
  credentials: 'Credentials',
  syncs: 'Sync history',
  contributions: 'Contributions',
  errors: 'Errors',
  audit: 'Audit',
};

export function parseTab(raw: string | null): TabId {
  if (raw !== null && (TAB_IDS as readonly string[]).includes(raw)) {
    return raw as TabId;
  }
  return 'overview';
}
