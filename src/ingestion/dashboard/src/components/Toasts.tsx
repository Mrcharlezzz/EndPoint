import { useState, type ReactElement } from 'react';
import { Icon, type IconName } from '@/components/ui';
import { cn } from '@/lib/cn';
import { useToastStore, type Toast, type ToastTone } from '@/lib/toasts';

const ICON_FOR_TONE: Record<ToastTone, IconName> = {
  info: 'clock',
  success: 'check',
  warn: 'alert',
  error: 'x-circle',
};

export function Toasts(): ReactElement {
  const toasts = useToastStore((s) => s.toasts);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <ToastRow key={t.id} toast={t} />
      ))}
    </div>
  );
}

function ToastRow({ toast }: { toast: Toast }): ReactElement {
  const dismiss = useToastStore((s) => s.dismiss);
  const [exiting, setExiting] = useState(false);
  const [paused, setPaused] = useState(false);

  function onDismiss(): void {
    setExiting(true);
    window.setTimeout(() => dismiss(toast.id), 160);
  }

  return (
    <div
      className={cn('toast', `tone-${toast.tone}`, exiting && 'exiting', paused && 'paused')}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <span className={cn('toast-icon', `tone-${toast.tone}`)} aria-hidden="true">
        <Icon name={ICON_FOR_TONE[toast.tone]} size={11} />
      </span>
      <div className="toast-body">
        <div className="toast-title">{toast.title}</div>
        {toast.message !== undefined && toast.message.length > 0 && (
          <div className="toast-msg">{toast.message}</div>
        )}
      </div>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss"
        onClick={onDismiss}
      >
        <Icon name="close" size={10} />
      </button>
      {toast.ttl > 0 && (
        <div className="toast-ttl" aria-hidden="true">
          <span
            className="toast-ttl-fill"
            style={{ animationDuration: `${toast.ttl}ms` }}
          />
        </div>
      )}
    </div>
  );
}
