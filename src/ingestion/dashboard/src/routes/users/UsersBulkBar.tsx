import type { ReactElement } from 'react';
import { Button, Icon } from '@/components/ui';
import {
  useBulkRefreshTeams,
  useForceRefreshTeams,
  usePauseUser,
} from '@/api/queries';

interface UsersBulkBarProps {
  selectedEmails: readonly string[];
  onClear: () => void;
}

export function UsersBulkBar({ selectedEmails, onClear }: UsersBulkBarProps): ReactElement | null {
  const pause = usePauseUser();
  const refreshOne = useForceRefreshTeams();
  const refreshAll = useBulkRefreshTeams();

  if (selectedEmails.length === 0) return null;

  async function onPauseAll(): Promise<void> {
    for (const email of selectedEmails) {
      try {
        await pause.mutateAsync({ email, reason: 'bulk action' });
      } catch {
        /* toast is handled globally by the MutationCache */
      }
    }
    onClear();
  }

  async function onRefreshTeams(): Promise<void> {
    if (selectedEmails.length > 5) {
      try {
        await refreshAll.mutateAsync({ reason: 'bulk refresh from users page' });
      } catch {
        /* handled globally */
      }
      onClear();
      return;
    }
    for (const email of selectedEmails) {
      try {
        await refreshOne.mutateAsync({ email, reason: 'bulk action' });
      } catch {
        /* handled globally */
      }
    }
    onClear();
  }

  return (
    <div className="users-bulkbar">
      <span className="tiny dim">{selectedEmails.length} selected</span>
      <span className="fill" />
      <Button size="sm" variant="ghost" onClick={() => void onPauseAll()}>
        <Icon name="pause" size={11} />
        pause
      </Button>
      <Button size="sm" variant="ghost" onClick={() => void onRefreshTeams()}>
        <Icon name="refresh" size={11} />
        refresh Teams tokens
      </Button>
      <Button size="sm" variant="ghost" onClick={onClear} aria-label="Clear selection">
        <Icon name="close" size={11} />
      </Button>
    </div>
  );
}
