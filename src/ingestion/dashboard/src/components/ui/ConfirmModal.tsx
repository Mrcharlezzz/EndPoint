import { useEffect, useState, type ReactElement, type ReactNode } from 'react';
import { Button } from './Button';
import { Input } from './Input';

interface ConfirmModalProps {
  open: boolean;
  title: ReactNode;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'default' | 'danger';
  /** When set, user must type this value exactly before confirm enables. */
  typedConfirm?: string;
  /** When true, user must also enter a reason. The reason is passed to onConfirm. */
  requireReason?: boolean;
  onConfirm: (reason?: string) => void | Promise<void>;
  onCancel: () => void;
  busy?: boolean;
  error?: string | null;
}

export function ConfirmModal(props: ConfirmModalProps): ReactElement | null {
  if (!props.open) return null;
  return <ConfirmModalInner {...props} />;
}

function ConfirmModalInner({
  title,
  body,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  typedConfirm,
  requireReason,
  onConfirm,
  onCancel,
  busy,
  error,
}: ConfirmModalProps): ReactElement {
  const [typed, setTyped] = useState('');
  const [reason, setReason] = useState('');

  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === 'Escape') onCancel();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const typedOk = typedConfirm === undefined || typed === typedConfirm;
  const reasonOk = requireReason !== true || reason.trim().length > 0;
  const canConfirm = typedOk && reasonOk && busy !== true;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-h">{title}</div>
        <div className="modal-b">
          {body}
          {typedConfirm !== undefined && (
            <label className="col gap-1">
              <span className="tiny muted">
                Type <span className="mono">{typedConfirm}</span> to confirm
              </span>
              <Input
                value={typed}
                onChange={(e) => setTyped(e.currentTarget.value)}
                mono
                autoFocus
              />
            </label>
          )}
          {requireReason === true && (
            <label className="col gap-1">
              <span className="tiny muted">Reason (required)</span>
              <Input
                value={reason}
                onChange={(e) => setReason(e.currentTarget.value)}
                autoFocus={typedConfirm === undefined}
              />
            </label>
          )}
          {error !== null && error !== undefined && error.length > 0 && (
            <div className="tiny text-err">{error}</div>
          )}
        </div>
        <div className="modal-f">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            variant={variant === 'danger' ? 'danger' : 'primary'}
            onClick={() => void onConfirm(requireReason === true ? reason.trim() : undefined)}
            disabled={!canConfirm}
          >
            {busy === true ? 'Working…' : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
