import type { ReactElement, ReactNode } from 'react';

export interface CounterItem {
  n: ReactNode;
  label: ReactNode;
}

export function Counters({ items }: { items: readonly CounterItem[] }): ReactElement {
  return (
    <div className="counters" style={{ gridTemplateColumns: `repeat(${items.length}, 1fr)` }}>
      {items.map((item, i) => (
        <div key={i} className="c">
          <span className="n">{item.n}</span>
          <span className="l">{item.label}</span>
        </div>
      ))}
    </div>
  );
}
