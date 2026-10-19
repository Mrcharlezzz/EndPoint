import { Clock } from "lucide-react";

import { formatDeadline } from "@/domain/deadline";
import type { Assignment } from "@/domain/entities";

type Props = {
  item: Assignment;
  timeFmt?: "12h" | "24h";
};

export function AssignmentMeta({ item, timeFmt = "24h" }: Props) {
  const dl = formatDeadline(item.deadline, timeFmt);
  return (
    <div className="card-meta-row">
      <span className="deadline" data-urgency={dl.urgency}>
        <Clock size={13} aria-hidden />
        {dl.text}
      </span>
    </div>
  );
}
