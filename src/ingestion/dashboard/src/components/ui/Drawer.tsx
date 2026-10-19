import type { HTMLAttributes, ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface DrawerProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode;
  className?: string;
}
export function Drawer({ children, className, ...rest }: DrawerProps): ReactElement {
  return (
    <aside className={cn('drawer', className)} {...rest}>
      {children}
    </aside>
  );
}

interface DrawerHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}
export function DrawerHeader({ title, subtitle, actions }: DrawerHeaderProps): ReactElement {
  return (
    <header className="drawer-h">
      <div className="title-row">
        <span className="id">{title}</span>
        {actions !== undefined && (
          <>
            <span className="spacer" style={{ flex: 1 }} />
            {actions}
          </>
        )}
      </div>
      {subtitle !== undefined && <div className="subtitle-row">{subtitle}</div>}
    </header>
  );
}

export function DrawerBody({ children }: { children: ReactNode }): ReactElement {
  return <div className="drawer-b">{children}</div>;
}

export function DrawerSectionHeading({ children }: { children: ReactNode }): ReactElement {
  return <div className="drawer-section-h">{children}</div>;
}
