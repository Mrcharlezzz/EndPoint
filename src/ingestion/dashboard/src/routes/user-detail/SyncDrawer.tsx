import type { ReactElement } from 'react';
import { Button, Drawer, DrawerBody, DrawerHeader, Icon, KV } from '@/components/ui';
import { useSync } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';

interface SyncDrawerProps {
  syncId: string | null;
  onClose: () => void;
}

export function SyncDrawer({ syncId, onClose }: SyncDrawerProps): ReactElement {
  if (syncId === null) {
    return (
      <Drawer className="ud-drawer">
        <div className="drawer-h">
          <span className="id dim">Select a sync run</span>
        </div>
        <DrawerBody>
          <p className="tiny dim">Click any row to inspect the sync record.</p>
        </DrawerBody>
      </Drawer>
    );
  }
  return <SyncDrawerInner syncId={syncId} onClose={onClose} />;
}

function pickStr(rec: Record<string, unknown>, key: string): string {
  const v = rec[key];
  if (v === null || v === undefined) return '—';
  if (typeof v === 'string') return v.length === 0 ? '—' : v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

function SyncDrawerInner({ syncId, onClose }: { syncId: string; onClose: () => void }): ReactElement {
  const q = useSync(syncId);
  const history = q.data?.history as Record<string, unknown> | undefined;
  const profile = q.data?.profile as Record<string, unknown> | null | undefined;
  return (
    <Drawer className="ud-drawer">
      <DrawerHeader
        title={
          <>
            <Button size="sm" variant="ghost" aria-label="Close drawer" onClick={onClose}>
              <Icon name="close" size={12} />
            </Button>
            <span className="mono tiny" style={{ marginLeft: 6 }}>
              sync {syncId.slice(0, 8)}
            </span>
          </>
        }
      />
      <DrawerBody>
        {q.isLoading && <span className="tiny dim">loading…</span>}
        {q.isError && <span className="tiny err">Failed to load sync.</span>}
        {history !== undefined && (
          <KV
            mono
            rows={[
              ['id', pickStr(history, 'id')],
              ['state', pickStr(history, 'state')],
              ['user', pickStr(history, 'user_email')],
              ['platform', pickStr(history, 'platform')],
              [
                'started',
                typeof history['started_at'] === 'string' ? formatRelative(history['started_at']) : '—',
              ],
              [
                'finished',
                typeof history['finished_at'] === 'string'
                  ? formatRelative(history['finished_at'])
                  : '—',
              ],
              ['error', pickStr(history, 'error')],
            ]}
          />
        )}
        {history !== undefined && (
          <section>
            <div className="drawer-section-h">Stats</div>
            <pre className="ud-json">{JSON.stringify(history['stats'] ?? {}, null, 2)}</pre>
          </section>
        )}
        {history !== undefined && (
          <section>
            <div className="drawer-section-h">Cursor (after)</div>
            <pre className="ud-json">{JSON.stringify(history['cursor_after'] ?? null, null, 2)}</pre>
          </section>
        )}
        {profile !== undefined && profile !== null && (
          <section>
            <div className="drawer-section-h">Profile</div>
            <pre className="ud-json">{JSON.stringify(profile, null, 2)}</pre>
          </section>
        )}
      </DrawerBody>
    </Drawer>
  );
}
