"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";

import { track } from "@/features/analytics/events";
import { Card } from "@/features/cards/Card";
import { EmptyState } from "@/features/cards/EmptyState";
import { SkeletonStream } from "@/features/cards/SkeletonCard";
import { useVisibleStore } from "@/features/dashboard/visibleStore";
import { useFiltersStore } from "@/features/filters/store";
import { hasUpcomingDeadline, matchesFilters } from "@/domain/filter";
import { byAssignmentRecentDesc, byDeadlineAsc } from "@/domain/sort";
import { useAssignments } from "@/lib/queries/useAssignments";
import { useReadState } from "@/lib/queries/useReadState";
import { useSession } from "@/lib/queries/useSession";

export function AssignmentsView() {
  useEffect(() => {
    track("dashboard.viewed", { dashboard: "assignments" });
  }, []);

  const { isAuthed } = useSession();
  const { data, isLoading, isError, refetch } = useAssignments();
  const { data: readMap } = useReadState({ enabled: isAuthed });
  const courses = useFiltersStore((s) => s.courses);
  const platform = useFiltersStore((s) => s.platform);
  const clear = useFiltersStore((s) => s.clear);
  const [otherOpen, setOtherOpen] = useState(false);

  const buckets = useMemo(() => {
    if (!data) return { other: [], upcoming: [], total: 0, hasFilters: false };
    const filtered = data.filter((a) =>
      matchesFilters(a, { courses, platform, type: null }),
    );
    const now = new Date();
    return {
      other: filtered.filter((a) => !hasUpcomingDeadline(a, now)).sort(byAssignmentRecentDesc),
      upcoming: filtered.filter((a) => hasUpcomingDeadline(a, now)).sort(byDeadlineAsc),
      total: filtered.length,
      hasFilters: courses.size > 0 || platform !== null,
    };
  }, [data, courses, platform]);

  const setVisibleIds = useVisibleStore((s) => s.setVisibleIds);
  useEffect(() => {
    if (!data) return;
    setVisibleIds([...buckets.other, ...buckets.upcoming].map((a) => a.id));
  }, [data, buckets, setVisibleIds]);

  if (isLoading) return <SkeletonStream count={4} />;

  if (isError) {
    return (
      <div className="error-banner" role="alert">
        <span>Couldn&apos;t load your assignments.</span>
        <button type="button" onClick={() => refetch()}>
          Retry
        </button>
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <EmptyState
        title="You're all caught up"
        body="No assignments yet. Once your courses are tracked and synced, they'll show up here."
      />
    );
  }

  if (buckets.total === 0 && buckets.hasFilters) {
    return (
      <EmptyState
        title="No assignments match these filters"
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
    <>
      <section className="dashboard-section">
        <header className="dashboard-section-header">
          <h2 className="dashboard-section-title">Upcoming</h2>
          <span className="dashboard-section-count">{buckets.upcoming.length}</span>
        </header>
        <div className="card-stream">
          {buckets.upcoming.map((item) => (
            <Card key={item.id} item={item} isRead={isRead(item.id)} />
          ))}
        </div>
      </section>

      {buckets.other.length > 0 ? (
        <section className="dashboard-section dashboard-section--overdue">
          <button
            type="button"
            className="overdue-toggle"
            aria-expanded={otherOpen}
            onClick={() => setOtherOpen((v) => !v)}
          >
            <ChevronRight size={14} className="overdue-chev" aria-hidden />
            <span className="overdue-title">General</span>
            <span className="overdue-count">{buckets.other.length}</span>
          </button>
          {otherOpen ? (
            <div className="card-stream" style={{ marginTop: 8 }}>
              {buckets.other.map((item) => (
                <Card key={item.id} item={item} isRead={isRead(item.id)} />
              ))}
            </div>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
