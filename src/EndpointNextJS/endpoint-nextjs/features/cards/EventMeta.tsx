import { Clock, MapPin } from "lucide-react";

import type { Event } from "@/domain/entities";
import { formatEventTime } from "@/domain/format-date";

type Props = {
  item: Event;
  timeFmt?: "12h" | "24h";
};

export function EventMeta({ item, timeFmt = "24h" }: Props) {
  const t = formatEventTime(item.startTime, item.endTime, timeFmt);
  if (!t.text && !item.location) return null;
  return (
    <div className="card-meta-row" style={t.past ? { opacity: 0.7 } : undefined}>
      {t.text ? (
        <span className="deadline" data-urgency={t.past ? "none" : "neutral"}>
          <Clock size={13} aria-hidden />
          {t.text}
        </span>
      ) : null}
      {item.location ? (
        <span className="location">
          <MapPin size={13} aria-hidden />
          {item.location}
        </span>
      ) : null}
    </div>
  );
}
