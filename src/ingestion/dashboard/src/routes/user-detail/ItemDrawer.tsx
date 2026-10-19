import type { ReactElement } from 'react';
import { Button, Drawer, DrawerBody, DrawerHeader, Icon, KV } from '@/components/ui';
import { useItemDetail } from '@/api/queries';
import { formatRelative } from '@/hooks/useRelativeTime';

interface ItemDrawerProps {
  itemId: string | null;
  onClose: () => void;
}

export function ItemDrawer({ itemId, onClose }: ItemDrawerProps): ReactElement {
  if (itemId === null) {
    return (
      <Drawer className="ud-drawer">
        <div className="drawer-h">
          <span className="id dim">Select an item</span>
        </div>
        <DrawerBody>
          <p className="tiny dim">Click any row to inspect its payload.</p>
        </DrawerBody>
      </Drawer>
    );
  }
  return <ItemDrawerInner itemId={itemId} onClose={onClose} />;
}

function ItemDrawerInner({
  itemId,
  onClose,
}: {
  itemId: string;
  onClose: () => void;
}): ReactElement {
  const q = useItemDetail(itemId);
  const data = q.data;
  return (
    <Drawer className="ud-drawer">
      <DrawerHeader
        title={
          <>
            <Button size="sm" variant="ghost" aria-label="Close drawer" onClick={onClose}>
              <Icon name="close" size={12} />
            </Button>
            <span className="mono tiny" style={{ marginLeft: 6 }}>
              item {itemId.slice(0, 8)}
            </span>
          </>
        }
      />
      <DrawerBody>
        {q.isLoading && <span className="tiny dim">loading…</span>}
        {q.isError && <span className="tiny err">Failed to load item.</span>}
        {data !== undefined && (
          <>
            <KV
              mono
              rows={[
                ['external_id', data.external_id],
                ['platform', data.platform],
                ['processing', data.processing_status],
                ['course_external_id', data.course_external_id ?? '—'],
                ['source_url', data.source_url ?? '—'],
                ['schema_version', data.schema_version],
                ['adapter_version', data.adapter_version || '—'],
                ['ingestion_mode', data.ingestion_mode],
                ['fetched_by', data.fetched_by_user_email ?? '—'],
                ['ingested', formatRelative(data.ingested_at)],
                ['updated', formatRelative(data.updated_at)],
                ['error', data.error_message ?? '—'],
              ]}
            />
            <section>
              <div className="drawer-section-h">Normalized payload</div>
              <pre className="ud-json">{JSON.stringify(data.payload, null, 2)}</pre>
            </section>
            <section>
              <div className="drawer-section-h">Raw payload</div>
              <pre className="ud-json">{JSON.stringify(data.raw_payload, null, 2)}</pre>
            </section>
          </>
        )}
      </DrawerBody>
    </Drawer>
  );
}
