export type ChannelStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

/* ==================== Channel event payloads ==================== */
/*
 * The daemon's EventBus publishes dict events on named channels. Events
 * carry a discriminator field (usually `event: "<noun>.<verb>"`) plus
 * channel-specific payload fields. The shapes below mirror the backend
 * publishers in ingestion/ingestion/daemon/runtime.py and
 * observability_loops.py — loose on payload details. Consumers should
 * narrow via the `event` discriminator and only assert on fields they
 * actually use.
 */

export interface SyncEventBase {
  event: string;
  user_email?: string;
  platform?: string;
  sync_id?: string;
  [key: string]: unknown;
}
export interface SyncStartedEvent extends SyncEventBase {
  event: 'sync.started';
}
export interface SyncFinishedEvent extends SyncEventBase {
  event: 'sync.finished';
}
export interface SyncFailedEvent extends SyncEventBase {
  event: 'sync.failed';
}
export type SyncsEvent = SyncStartedEvent | SyncFinishedEvent | SyncFailedEvent | SyncEventBase;

export interface LogEvent {
  timestamp: string;
  level: string;
  message: string;
  user_email?: string | null;
  platform?: string | null;
  scope?: string | null;
  sync_id?: string | null;
  context?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ErrorEvent {
  event?: string;
  timestamp?: string;
  user_email?: string | null;
  platform?: string | null;
  sync_id?: string | null;
  code?: string;
  message?: string;
  severity?: 'info' | 'warn' | 'error' | 'critical';
  [key: string]: unknown;
}

export interface AlertEvent {
  event: string;
  rule_id?: number;
  firing_id?: number;
  fired_at?: string;
  resolved_at?: string | null;
  severity?: string;
  [key: string]: unknown;
}

export interface MetricsSnapshot {
  event?: string;
  timestamp?: string;
  active_syncs?: number;
  queued?: number;
  dead_letter?: number;
  [key: string]: unknown;
}

export interface ChannelEventMap {
  '/ws/syncs': SyncsEvent;
  '/ws/logs': LogEvent;
  '/ws/errors': ErrorEvent;
  '/ws/alerts': AlertEvent;
  '/ws/metrics': MetricsSnapshot;
}

export type ChannelPath = keyof ChannelEventMap;
export type EventFor<P extends string> = P extends ChannelPath
  ? ChannelEventMap[P]
  : Record<string, unknown>;

/* ==================== Channel filters ==================== */

export interface SyncsFilter {
  user_email?: string;
  platform?: string;
  state?: 'running' | 'completed' | 'failed' | 'cancelled';
}
export interface LogsFilter {
  level?: 'trace' | 'debug' | 'info' | 'warn' | 'error';
  user_email?: string;
  platform?: string;
  scope?: string;
  sync_id?: string;
}
export interface ErrorsFilter {
  severity?: 'info' | 'warn' | 'error' | 'critical';
  platform?: string;
  user_email?: string;
  code?: string;
  recoverable?: boolean;
}
export interface AlertsFilter {
  severity?: 'info' | 'warn' | 'critical';
  rule_id?: number;
}
export type MetricsFilter = Record<string, never>;

export interface ChannelFilterMap {
  '/ws/syncs': SyncsFilter;
  '/ws/logs': LogsFilter;
  '/ws/errors': ErrorsFilter;
  '/ws/alerts': AlertsFilter;
  '/ws/metrics': MetricsFilter;
}

export type FilterFor<P extends string> = P extends ChannelPath
  ? ChannelFilterMap[P]
  : Record<string, string | number | boolean | null>;

/* ==================== Legacy aliases ==================== */
export type ChannelEvent = SyncsEvent | LogEvent | ErrorEvent | AlertEvent | MetricsSnapshot;
export type Filter = Record<string, string | number | boolean | null>;
