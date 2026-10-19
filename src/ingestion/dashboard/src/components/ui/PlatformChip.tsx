import type { ReactElement } from 'react';

const COLORS: Record<string, string> = {
  teams: 'oklch(0.40 0.12 280)',
  moodle: 'oklch(0.38 0.10 40)',
  telegram: 'oklch(0.40 0.12 230)',
};

interface PlatformChipProps {
  name: string;
}

export function PlatformChip({ name }: PlatformChipProps): ReactElement {
  const bg = COLORS[name] ?? 'var(--surface-3)';
  return (
    <span
      className="chip"
      style={{
        background: 'transparent',
        color: 'var(--text-2)',
        border: '1px solid var(--border)',
        paddingLeft: 4,
      }}
    >
      <span style={{ width: 7, height: 7, borderRadius: 2, background: bg }} />
      {name}
    </span>
  );
}
