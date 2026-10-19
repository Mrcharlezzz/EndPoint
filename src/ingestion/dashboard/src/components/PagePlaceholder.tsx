import type { ReactElement, ReactNode } from 'react';

interface PagePlaceholderProps {
  title: string;
  subtitle?: string;
  children?: ReactNode;
}

export function PagePlaceholder({
  title,
  subtitle,
  children,
}: PagePlaceholderProps): ReactElement {
  return (
    <>
      <header className="page-header">
        <h1>{title}</h1>
        {subtitle !== undefined && (
          <>
            <span className="crumb">·</span>
            <span className="crumb">{subtitle}</span>
          </>
        )}
      </header>
      <div className="page-body">
        {children ?? <p className="dim tiny">Not yet implemented.</p>}
      </div>
    </>
  );
}
