import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { QueryClient, UseMutationResult, UseQueryResult } from '@tanstack/react-query';
import { api } from './client';
import type { components, paths } from './schema';
import { getErrorMessage } from '@/lib/errors';

type Schemas = components['schemas'];

export type UserListItem = Schemas['UserListItem'];
export type UserDetail = Schemas['UserDetail'];
export type UserStats = Schemas['UserStats'];
export type UserItemRow = Schemas['UserItemRow'];
export type ItemDetail = Schemas['ItemDetail'];
export type CredentialSummary = Schemas['CredentialSummary'];
export type CredentialDetail = Schemas['CredentialDetail'];
export type SyncHistoryItem = Schemas['SyncHistoryItem'];
export type SyncDetailResponse = Schemas['SyncDetailResponse'];
export type ErrorsResponse = Schemas['ErrorsResponse'];
export type ErrorItem = Schemas['ErrorItem'];
export type LogEntry = Schemas['LogEntry'];
export type ProcessorSummaryResponse = Schemas['ProcessorSummaryResponse'];
export type ProcessorErrorsResponse = Schemas['ProcessorErrorsResponse'];
export type ProcessorErrorItem = Schemas['ProcessorErrorItem'];
export type ProcessorWindowResetResult = Schemas['ProcessorWindowResetResult'];
export type AuditEntry = Schemas['AuditEntry'];
export type AlertRuleOut = Schemas['AlertRuleOut'];
export type AlertFiring = Schemas['AlertFiring'];
export type DaemonConfig = Schemas['DaemonConfig'];
export type OverlayMap = Schemas['OverlayMap'];
export type StorageStats = Schemas['StorageStats'];
export type AboutResponse = Schemas['ingestion__daemon__api__routers__settings__AboutResponse'];
export type RetentionResponse = Schemas['RetentionResponse'];
export type AdaptersHealthResponse = Schemas['AdaptersHealthResponse'];

type Query<P extends keyof paths, M extends 'get' = 'get'> = paths[P][M] extends {
  parameters: { query?: infer Q };
}
  ? Q
  : never;

export type SyncsListParams = Query<'/api/syncs'>;
export type ErrorsListParams = Query<'/api/errors'>;
export type LogsListParams = Query<'/api/logs'>;
export type AuditListParams = Query<'/api/audit'>;
export type AlertFiringsParams = Query<'/api/alerts/firings'>;
export type UserItemsParams = Query<'/api/users/{email}/items'>;

export const queryKeys = {
  whoami: ['whoami'] as const,
  users: ['users'] as const,
  user: (email: string) => ['users', email] as const,
  userStats: (email: string) => ['users', email, 'stats'] as const,
  userItems: (email: string, params?: UserItemsParams) =>
    ['users', email, 'items', params ?? null] as const,
  item: (id: string) => ['items', id] as const,
  syncs: (params?: SyncsListParams) => ['syncs', params ?? null] as const,
  sync: (id: string) => ['syncs', id] as const,
  errors: (params?: ErrorsListParams) => ['errors', params ?? null] as const,
  processorSummary: ['processor', 'summary'] as const,
  processorErrors: (limit = 200) => ['processor', 'errors', limit] as const,
  credentials: ['credentials'] as const,
  credential: (email: string, platform: string) => ['credentials', email, platform] as const,
  logs: (params?: LogsListParams) => ['logs', params ?? null] as const,
  audit: (params?: AuditListParams) => ['audit', params ?? null] as const,
  alertRules: ['alerts', 'rules'] as const,
  alertFirings: (params?: AlertFiringsParams) => ['alerts', 'firings', params ?? null] as const,
  config: ['config'] as const,
  configOverlay: ['config', 'overlay'] as const,
  storage: ['settings', 'storage'] as const,
  about: ['settings', 'about'] as const,
  retention: ['settings', 'retention'] as const,
  adaptersHealth: ['settings', 'adapters', 'health'] as const,
  scheduleNext: ['schedule', 'next'] as const,
} as const;

async function unwrap<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise;
  if (error !== undefined || data === undefined) {
    throw new Error(`${response.status} ${response.statusText}: ${getErrorMessage(error)}`);
  }
  return data;
}

function invalidate(qc: QueryClient, keys: readonly (readonly unknown[])[]): void {
  for (const key of keys) void qc.invalidateQueries({ queryKey: [...key] });
}

/* ==================== Auth ==================== */

export function useWhoami(): UseQueryResult<unknown> {
  return useQuery({
    queryKey: queryKeys.whoami,
    queryFn: () => unwrap(api.GET('/api/auth/whoami', {})),
    retry: false,
  });
}

export function useLogin(): UseMutationResult<unknown, Error, { password: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ password }) => unwrap(api.POST('/api/auth/login', { body: { password } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.whoami });
    },
  });
}

export function useLogout(): UseMutationResult<unknown, Error, void> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST('/api/auth/logout', {})),
    onSettled: () => {
      qc.clear();
    },
  });
}

/* ==================== Users ==================== */

export function useUsers(): UseQueryResult<UserListItem[]> {
  return useQuery({
    queryKey: queryKeys.users,
    queryFn: () => unwrap(api.GET('/api/users', {})),
  });
}

export function useUser(email: string): UseQueryResult<UserDetail> {
  return useQuery({
    queryKey: queryKeys.user(email),
    queryFn: () => unwrap(api.GET('/api/users/{email}', { params: { path: { email } } })),
    enabled: email.length > 0,
  });
}

export function useUserStats(email: string): UseQueryResult<UserStats> {
  return useQuery({
    queryKey: queryKeys.userStats(email),
    queryFn: () =>
      unwrap(api.GET('/api/users/{email}/stats', { params: { path: { email } } })),
    enabled: email.length > 0,
  });
}

export function useUserItems(
  email: string,
  params?: UserItemsParams,
): UseQueryResult<UserItemRow[]> {
  return useQuery({
    queryKey: queryKeys.userItems(email, params),
    queryFn: () =>
      unwrap(
        api.GET('/api/users/{email}/items', {
          params: { path: { email }, query: params ?? {} },
        }),
      ),
    enabled: email.length > 0,
  });
}

export function useItemDetail(itemId: string): UseQueryResult<ItemDetail> {
  return useQuery({
    queryKey: queryKeys.item(itemId),
    queryFn: () =>
      unwrap(api.GET('/api/items/{item_id}', { params: { path: { item_id: itemId } } })),
    enabled: itemId.length > 0,
  });
}

/* ==================== Syncs ==================== */

export function useSyncs(params?: SyncsListParams): UseQueryResult<SyncHistoryItem[]> {
  return useQuery({
    queryKey: queryKeys.syncs(params),
    queryFn: () => unwrap(api.GET('/api/syncs', { params: { query: params ?? {} } })),
  });
}

export function useSync(syncId: string): UseQueryResult<SyncDetailResponse> {
  return useQuery({
    queryKey: queryKeys.sync(syncId),
    queryFn: () =>
      unwrap(api.GET('/api/syncs/{sync_id}', { params: { path: { sync_id: syncId } } })),
    enabled: syncId.length > 0,
  });
}

/* ==================== Errors ==================== */

export function useErrors(params?: ErrorsListParams): UseQueryResult<ErrorsResponse> {
  return useQuery({
    queryKey: queryKeys.errors(params),
    queryFn: () => unwrap(api.GET('/api/errors', { params: { query: params ?? {} } })),
  });
}

export function useProcessorSummary(): UseQueryResult<ProcessorSummaryResponse> {
  return useQuery({
    queryKey: queryKeys.processorSummary,
    queryFn: () => unwrap(api.GET('/api/processor/summary', {})),
    staleTime: 15_000,
    refetchInterval: 15_000,
  });
}

export function useProcessorErrors(limit = 200): UseQueryResult<ProcessorErrorsResponse> {
  return useQuery({
    queryKey: queryKeys.processorErrors(limit),
    queryFn: () =>
      unwrap(api.GET('/api/processor/errors', { params: { query: { limit } } })),
    staleTime: 15_000,
    refetchInterval: 15_000,
  });
}

/* ==================== Credentials ==================== */

export function useCredentials(): UseQueryResult<CredentialSummary[]> {
  return useQuery({
    queryKey: queryKeys.credentials,
    queryFn: () => unwrap(api.GET('/api/credentials', {})),
  });
}

export function useCredentialDetail(
  email: string,
  platform: string,
): UseQueryResult<CredentialDetail> {
  return useQuery({
    queryKey: queryKeys.credential(email, platform),
    queryFn: () =>
      unwrap(
        api.GET('/api/credentials/{email}/{platform}', {
          params: { path: { email, platform } },
        }),
      ),
    enabled: email.length > 0 && platform.length > 0,
  });
}

/* ==================== Logs ==================== */

export function useLogs(params?: LogsListParams): UseQueryResult<LogEntry[]> {
  return useQuery({
    queryKey: queryKeys.logs(params),
    queryFn: () => unwrap(api.GET('/api/logs', { params: { query: params ?? {} } })),
  });
}

/* ==================== Audit ==================== */

export function useAudit(params?: AuditListParams): UseQueryResult<AuditEntry[]> {
  return useQuery({
    queryKey: queryKeys.audit(params),
    queryFn: () => unwrap(api.GET('/api/audit', { params: { query: params ?? {} } })),
  });
}

/* ==================== Alerts ==================== */

export function useAlertRules(): UseQueryResult<AlertRuleOut[]> {
  return useQuery({
    queryKey: queryKeys.alertRules,
    queryFn: () => unwrap(api.GET('/api/alerts/rules', {})),
  });
}

export function useAlertFirings(params?: AlertFiringsParams): UseQueryResult<AlertFiring[]> {
  return useQuery({
    queryKey: queryKeys.alertFirings(params),
    queryFn: () => unwrap(api.GET('/api/alerts/firings', { params: { query: params ?? {} } })),
  });
}

/* ==================== Config ==================== */

export function useConfig(): UseQueryResult<DaemonConfig> {
  return useQuery({
    queryKey: queryKeys.config,
    queryFn: () => unwrap(api.GET('/api/config', {})),
  });
}

export function useConfigOverlay(): UseQueryResult<OverlayMap> {
  return useQuery({
    queryKey: queryKeys.configOverlay,
    queryFn: () => unwrap(api.GET('/api/config/overlay', {})),
  });
}

/* ==================== Settings ==================== */

export function useStorageStats(): UseQueryResult<StorageStats> {
  return useQuery({
    queryKey: queryKeys.storage,
    queryFn: () => unwrap(api.GET('/api/settings/storage', {})),
  });
}

export function useAbout(): UseQueryResult<AboutResponse> {
  return useQuery({
    queryKey: queryKeys.about,
    queryFn: () => unwrap<AboutResponse>(api.GET('/api/settings/about', {})),
  });
}

export function useRetention(): UseQueryResult<RetentionResponse> {
  return useQuery({
    queryKey: queryKeys.retention,
    queryFn: () => unwrap(api.GET('/api/settings/retention', {})),
  });
}

export function useAdaptersHealth(): UseQueryResult<AdaptersHealthResponse> {
  return useQuery({
    queryKey: queryKeys.adaptersHealth,
    queryFn: () => unwrap(api.GET('/api/settings/adapters/health', {})),
    // Server caches for 60s; match that cadence on the client.
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
}

/* ==================== Mutations ==================== */

export function usePauseUser(): UseMutationResult<unknown, Error, { email: string; reason?: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/pause', {
          params: { path: { email } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.users, queryKeys.user(vars.email), queryKeys.audit()]);
    },
  });
}

export function useUnpauseUser(): UseMutationResult<
  unknown,
  Error,
  { email: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/unpause', {
          params: { path: { email } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.users, queryKeys.user(vars.email), queryKeys.audit()]);
    },
  });
}

export function useForceRefreshTeams(): UseMutationResult<
  unknown,
  Error,
  { email: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/teams/refresh', {
          params: { path: { email } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.credentials, queryKeys.user(vars.email), queryKeys.audit()]);
    },
  });
}

export function useBulkRefreshTeams(): UseMutationResult<unknown, Error, { reason?: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reason }) =>
      unwrap(api.POST('/api/actions/teams/refresh_all', { body: { reason: reason ?? null } })),
    onSuccess: () => {
      invalidate(qc, [queryKeys.credentials, queryKeys.audit()]);
    },
  });
}

export function useResetProcessorWindow(): UseMutationResult<
  ProcessorWindowResetResult,
  Error,
  { reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reason }) =>
      unwrap(api.POST('/api/actions/processor/window/reset', { body: { reason: reason ?? null } })),
    onSuccess: () => {
      invalidate(qc, [
        queryKeys.processorSummary,
        queryKeys.processorErrors(200),
        queryKeys.audit(),
      ]);
    },
  });
}

export function useScheduleNext(): UseQueryResult<Schemas['ScheduleNextResponse']> {
  return useQuery({
    queryKey: queryKeys.scheduleNext,
    queryFn: () => unwrap(api.GET('/api/schedule/next', {})),
  });
}

export type FullResyncPlatform = 'teams' | 'moodle';

export function useFullResync(): UseMutationResult<
  Schemas['FullResyncResult'],
  Error,
  { reason: string; platforms: FullResyncPlatform[] }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reason, platforms }) =>
      unwrap(
        api.POST('/api/ingest/full_resync', {
          body: { confirm: 'FULL_RESYNC', reason, platforms },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.syncs(), queryKeys.credentials, queryKeys.audit()]);
    },
  });
}

export function useForceSync(): UseMutationResult<
  unknown,
  Error,
  { email: string; platform: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, platform, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/{platform}/force_sync', {
          params: { path: { email, platform } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.user(vars.email), queryKeys.audit()]);
    },
  });
}

export function useForceSyncAll(): UseMutationResult<
  unknown,
  Error,
  { email: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/force_sync_all', {
          params: { path: { email } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.user(vars.email), queryKeys.userStats(vars.email), queryKeys.audit()]);
    },
  });
}

export function useResetCursor(): UseMutationResult<
  unknown,
  Error,
  { email: string; platform: string; confirm: string; reason: string; scope?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, platform, confirm, reason, scope }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/{platform}/reset_cursor', {
          params: { path: { email, platform } },
          body: { confirm, reason, scope: scope ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [queryKeys.user(vars.email), queryKeys.audit()]);
    },
  });
}

export function useMarkExpired(): UseMutationResult<
  unknown,
  Error,
  { email: string; platform: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, platform, reason }) =>
      unwrap(
        api.POST('/api/actions/users/{email}/{platform}/mark_expired', {
          params: { path: { email, platform } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: (_d, vars) => {
      invalidate(qc, [
        queryKeys.credentials,
        queryKeys.credential(vars.email, vars.platform),
        queryKeys.user(vars.email),
        queryKeys.audit(),
      ]);
    },
  });
}

export function useClearDeadLetter(): UseMutationResult<
  unknown,
  Error,
  { email: string; platform: string; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, platform, reason }) =>
      unwrap(
        api.POST('/api/actions/dead_letter/{email}/{platform}/clear', {
          params: { path: { email, platform } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.audit()]);
    },
  });
}

export function useSetDrainMode(): UseMutationResult<
  unknown,
  Error,
  { enabled: boolean; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ enabled, reason }) =>
      unwrap(
        api.POST('/api/actions/scheduler/drain', {
          body: { enabled, reason: reason ?? null },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.config, queryKeys.configOverlay, queryKeys.audit()]);
    },
  });
}

export function useEmergencyStop(): UseMutationResult<
  unknown,
  Error,
  { confirm: string; reason: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ confirm, reason }) =>
      unwrap(api.POST('/api/actions/scheduler/emergency_stop', { body: { confirm, reason } })),
    onSuccess: () => {
      invalidate(qc, [queryKeys.config, queryKeys.configOverlay, queryKeys.audit()]);
    },
  });
}

export function usePurgeItem(): UseMutationResult<
  unknown,
  Error,
  { itemId: string; confirm: string; reason: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ itemId, confirm, reason }) =>
      unwrap(
        api.POST('/api/actions/content/{item_id}/purge', {
          params: { path: { item_id: itemId } },
          body: { confirm, reason },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.audit()]);
    },
  });
}

export function useCompactStorage(): UseMutationResult<unknown, Error, { reason?: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ reason }) =>
      unwrap(api.POST('/api/actions/storage/compact', { body: { reason: reason ?? null } })),
    onSuccess: () => {
      invalidate(qc, [queryKeys.storage, queryKeys.audit()]);
    },
  });
}

export function useSetAlertRuleEnabled(): UseMutationResult<
  unknown,
  Error,
  { ruleId: number; enabled: boolean }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ruleId, enabled }) =>
      unwrap(
        api.PATCH('/api/alerts/rules/{rule_id}/enabled', {
          params: { path: { rule_id: ruleId } },
          body: { enabled },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.alertRules, queryKeys.audit()]);
    },
  });
}

export function useMuteAlertRule(): UseMutationResult<
  unknown,
  Error,
  { ruleId: number; muted: boolean; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ruleId, muted, reason }) =>
      unwrap(
        api.PATCH('/api/actions/alerts/rules/{rule_id}/mute', {
          params: { path: { rule_id: ruleId } },
          body: { enabled: muted, reason: reason ?? null },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.alertRules, queryKeys.audit()]);
    },
  });
}

export function useSetConfigOverlay(): UseMutationResult<
  unknown,
  Error,
  { key: string; value: unknown; reason?: string }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value, reason }) =>
      unwrap(
        api.PUT('/api/config/overlay/{key}', {
          params: { path: { key } },
          body: { value, reason: reason ?? null },
        }),
      ),
    onSuccess: () => {
      invalidate(qc, [queryKeys.config, queryKeys.configOverlay, queryKeys.audit()]);
    },
  });
}

export function useClearConfigOverlay(): UseMutationResult<unknown, Error, { key: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key }) =>
      unwrap(api.DELETE('/api/config/overlay/{key}', { params: { path: { key } } })),
    onSuccess: () => {
      invalidate(qc, [queryKeys.config, queryKeys.configOverlay, queryKeys.audit()]);
    },
  });
}
