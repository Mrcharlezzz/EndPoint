import type { ReactElement } from 'react';
import { cn } from '@/lib/cn';

interface ToggleProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  ariaLabel?: string;
  className?: string;
}

export function Toggle({ checked, onChange, ariaLabel, className }: ToggleProps): ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      className={cn('toggle', checked && 'on', className)}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}
