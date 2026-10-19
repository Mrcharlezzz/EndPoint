"use client";

import { ExternalLink } from "lucide-react";

import { track } from "@/features/analytics/events";
import { tintFor } from "@/domain/course";
import { isOverdue } from "@/domain/filter";
import type { Item } from "@/domain/entities";
import { formatPublished } from "@/domain/format-date";
import { useMarkRead } from "@/lib/queries/useReadState";
import { useSession } from "@/lib/queries/useSession";

import { AssignmentMeta } from "./AssignmentMeta";
import { EventMeta } from "./EventMeta";
import { PlatformIcon } from "./PlatformIcon";

const TITLE_FALLBACK: Record<Item["type"], string> = {
  assignment: "Assignment",
  announcement: "Announcement",
  event: "Event",
};

type Props = {
  item: Item;
  isRead?: boolean;
  timeFmt?: "12h" | "24h";
};

export function Card({ item, isRead = false, timeFmt = "24h" }: Props) {
  const tint = tintFor(item.course.id);
  const overdue = item.type === "assignment" && isOverdue(item);
  const title = item.title || TITLE_FALLBACK[item.type];
  const hasBody = !!item.body;
  const pillType = item.type;
  const pillLabel = overdue ? "closed" : item.type;

  const { isAuthed } = useSession();
  const { mutate: markReadMutate } = useMarkRead();

  const handleClick = () => {
    if (!isAuthed || isRead) return;
    markReadMutate([item.id]);
    track("item.marked_read", { count: 1 });
  };

  return (
    <article
      className="card"
      data-read={isRead}
      data-overdue={overdue || undefined}
      onClick={handleClick}
    >
      {isAuthed && !isRead ? <span className="unread-dot" aria-label="Unread" /> : null}

      <div
        className="card-overline"
        style={{ ["--course-tint" as never]: tint }}
      >
        <span>{item.course.code || item.course.name}</span>
        {item.course.code ? (
          <>
            <span style={{ color: "var(--text-faint)" }}>·</span>
            <span style={{ fontFamily: "var(--font-sans)", textTransform: "none" }}>
              {item.course.name}
            </span>
          </>
        ) : null}
      </div>

      <h3 className="card-title" style={!hasBody ? { fontWeight: 600 } : undefined}>
        {title}
      </h3>
      {hasBody ? <p className="card-body">{item.body}</p> : null}
      {!hasBody && item.author ? (
        <p className="card-byline">Posted by {item.author}</p>
      ) : null}

      {item.type === "assignment" ? (
        <AssignmentMeta item={item} timeFmt={timeFmt} />
      ) : null}
      {item.type === "event" ? <EventMeta item={item} timeFmt={timeFmt} /> : null}

      <div className="card-footer">
        <span>{formatPublished(item.publishedAt)}</span>
        <span style={{ color: "var(--text-faint)" }}>·</span>
        <span className="entity-pill" data-type={pillType}>
          {pillLabel}
        </span>
        <div className="card-footer-right">
          <PlatformIcon platform={item.platform} />
          {item.sourceUrl ? (
            <a
              className="source-btn"
              href={item.sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => {
                e.stopPropagation();
                if (isAuthed && !isRead) {
                  markReadMutate([item.id]);
                  track("item.marked_read", { count: 1 });
                }
              }}
            >
              Open source
              <ExternalLink size={11} aria-hidden />
            </a>
          ) : null}
        </div>
      </div>
    </article>
  );
}
