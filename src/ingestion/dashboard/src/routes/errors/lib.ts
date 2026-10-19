import type { ErrorItem } from '@/api/queries';

export type ErrorKind = 'dead_letter' | 'failed_sync' | 'expired_credential';

export interface FilterCounts {
  all: number;
  dead_letter: number;
  failed_sync: number;
  expired_credential: number;
}

export function countByKind(rows: ErrorItem[]): FilterCounts {
  const counts: FilterCounts = {
    all: rows.length,
    dead_letter: 0,
    failed_sync: 0,
    expired_credential: 0,
  };
  for (const row of rows) {
    if (row.kind === 'dead_letter') counts.dead_letter += 1;
    else if (row.kind === 'failed_sync') counts.failed_sync += 1;
    else if (row.kind === 'expired_credential') counts.expired_credential += 1;
  }
  return counts;
}

export const KIND_LABELS: Record<string, string> = {
  dead_letter: 'Dead letter',
  failed_sync: 'Failed sync',
  expired_credential: 'Expired credential',
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function matchesSearch(row: ErrorItem, query: string): boolean {
  if (query.length === 0) return true;
  const needle = query.toLowerCase();
  const haystack = [
    row.user_email ?? '',
    row.platform ?? '',
    row.kind,
    row.error ?? '',
    row.sync_id ?? '',
  ]
    .join(' ')
    .toLowerCase();
  return haystack.includes(needle);
}

/**
 * Build a GlitchTip search deep-link for this event.
 *
 * GlitchTip's root URL lands on the org selector, not on issues. The
 * real target is ``{base}/{org_slug}/issues/?query=...`` — tags set
 * via the daemon's sentry ``before_send`` processor (user_email /
 * platform / sync_id) make these searchable filters.
 */
export function glitchtipDeepLink(base: string, orgSlug: string, row: ErrorItem): string {
  if (base === '' || orgSlug === '') return '';
  const root = base.replace(/\/$/, '');
  const issuesUrl = `${root}/${orgSlug}/issues/`;
  const terms: string[] = [];
  if (row.user_email !== null && row.user_email !== undefined && row.user_email !== '') {
    terms.push(`user_email:${row.user_email}`);
  }
  if (row.platform !== null && row.platform !== undefined && row.platform !== '') {
    terms.push(`platform:${row.platform}`);
  }
  if (row.sync_id !== null && row.sync_id !== undefined && row.sync_id !== '') {
    terms.push(`sync_id:${row.sync_id}`);
  }
  const query = terms.join(' ');
  if (query === '') return issuesUrl;
  return `${issuesUrl}?query=${encodeURIComponent(query)}`;
}

/** Plain-issues URL for the page-header "Open GlitchTip" button. */
export function glitchtipIssuesUrl(base: string, orgSlug: string): string {
  if (base === '' || orgSlug === '') return '';
  const root = base.replace(/\/$/, '');
  return `${root}/${orgSlug}/issues/`;
}
