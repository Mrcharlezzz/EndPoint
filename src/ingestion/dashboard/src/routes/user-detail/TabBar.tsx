import type { ReactElement } from 'react';
import { FilterPill } from '@/components/ui';
import { TAB_IDS, TAB_LABELS, type TabId } from './lib';

interface TabBarProps {
  active: TabId;
  onChange: (next: TabId) => void;
  contributionsCount?: number | undefined;
}

export function TabBar({ active, onChange, contributionsCount }: TabBarProps): ReactElement {
  return (
    <nav className="ud-tabs" aria-label="User detail tabs">
      {TAB_IDS.map((id) => {
        const count = id === 'contributions' ? contributionsCount : undefined;
        const countProp = count === undefined ? {} : { count };
        return (
          <FilterPill
            key={id}
            active={active === id}
            onClick={() => onChange(id)}
            {...countProp}
          >
            {TAB_LABELS[id]}
          </FilterPill>
        );
      })}
    </nav>
  );
}
