import type { ReactNode } from "react";
import { Inbox } from "lucide-react";

type Props = {
  icon?: ReactNode;
  title: string;
  body?: string;
  action?: ReactNode;
};

export function EmptyState({ icon, title, body, action }: Props) {
  return (
    <div className="empty-state" role="status">
      <div className="empty-state-glyph" aria-hidden>
        {icon ?? <Inbox size={26} strokeWidth={1.5} />}
      </div>
      <p className="empty-state-title">{title}</p>
      {body ? <p className="empty-state-body">{body}</p> : null}
      {action ? <div>{action}</div> : null}
    </div>
  );
}
