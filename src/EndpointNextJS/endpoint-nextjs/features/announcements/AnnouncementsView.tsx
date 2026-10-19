"use client";

import { useEffect, useMemo } from "react";

import { track } from "@/features/analytics/events";
import { Card } from "@/features/cards/Card";
import { EmptyState } from "@/features/cards/EmptyState";
import { SkeletonStream } from "@/features/cards/SkeletonCard";
import { useVisibleStore } from "@/features/dashboard/visibleStore";
import { useFiltersStore } from "@/features/filters/store";
import { matchesFilters } from "@/domain/filter";
import { useAnnouncementsFeed } from "@/lib/queries/useAnnouncementsFeed";
import { useReadState } from "@/lib/queries/useReadState";
import { useSession } from "@/lib/queries/useSession";

export function AnnouncementsView() {
  useEffect(() => {
    track("dashboard.viewed", { dashboard: "announcements" });
  }, []);

  const { isAuthed } = useSession();
  const { data, isLoading, isError } = useAnnouncementsFeed();
  const { data: readMap } = useReadState({ enabled: isAuthed });
  const courses = useFiltersStore((s) => s.courses);
  const platform = useFiltersStore((s) => s.platform);
  const type = useFiltersStore((s) => s.type);
  const clear = useFiltersStore((s) => s.clear);

  const filtered = useMemo(() => {
    if (!data) return null;
    return data.filter((item) => matchesFilters(item, { courses, platform, type }));
  }, [data, courses, platform, type]);

  const hasFilters = courses.size > 0 || platform !== null || type !== null;

  const setVisibleIds = useVisibleStore((s) => s.setVisibleIds);
  useEffect(() => {
    if (filtered) setVisibleIds(filtered.map((i) => i.id));
  }, [filtered, setVisibleIds]);

  if (isLoading) return <SkeletonStream count={4} />;

  if (isError) {
    return (
      <div className="error-banner" role="alert">
        <span>Couldn&apos;t load your announcements.</span>
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <EmptyState
        title="No announcements yet"
        body="Posts from your tracked courses will show up here as they arrive."
      />
    );
  }

  if (filtered && filtered.length === 0 && hasFilters) {
    return (
      <EmptyState
        title="No items match these filters"
        body="Try removing a filter to see more results."
        action={
          <button type="button" className="btn-primary" onClick={clear}>
            Clear filters
          </button>
        }
      />
    );
  }

  const isRead = (id: string) => Boolean(readMap?.[id]);

  return (
    <section className="dashboard-section">
      <div className="card-stream">
        {(filtered ?? data).map((item) => (
          <Card key={`${item.type}-${item.id}`} item={item} isRead={isRead(item.id)} />
        ))}
      </div>
    </section>
  );
}
