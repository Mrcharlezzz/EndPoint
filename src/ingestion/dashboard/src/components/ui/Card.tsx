import type { CSSProperties, ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface CardProps {
  children: ReactNode;
  className?: string;
  inset?: boolean;
}
export function Card({ children, className, inset }: CardProps): ReactElement {
  return <div className={cn('card', inset && 'inset', className)}>{children}</div>;
}

interface CardHeaderProps {
  title?: ReactNode;
  right?: ReactNode;
  sub?: ReactNode;
  className?: string;
  children?: ReactNode;
}
export function CardHeader({
  title,
  right,
  sub,
  className,
  children,
}: CardHeaderProps): ReactElement {
  return (
    <div className={cn('card-h', className)}>
      {title !== undefined && <span className="title">{title}</span>}
      {sub !== undefined && <span className="sub">{sub}</span>}
      {children}
      {right !== undefined && (
        <>
          <span className="spacer" />
          {right}
        </>
      )}
    </div>
  );
}

export function CardBody({
  children,
  flush,
  className,
  style,
}: {
  children: ReactNode;
  flush?: boolean;
  className?: string;
  style?: CSSProperties;
}): ReactElement {
  return (
    <div className={cn('card-b', flush && 'flush', className)} style={style}>
      {children}
    </div>
  );
}
