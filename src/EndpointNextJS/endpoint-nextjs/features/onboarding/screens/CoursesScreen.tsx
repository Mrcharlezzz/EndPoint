"use client";

import { useMemo } from "react";

import type { Course } from "@/domain/course";
import { tintFor } from "@/domain/course";
import {
  useCatalog,
  useCourses,
  useSetTrackedCourses,
} from "@/lib/queries/useCourses";

type Props = {
  onContinueAction: () => void;
  onBackAction: () => void;
  onSkipAction: () => void;
};

export function CoursesScreen({ onContinueAction, onBackAction, onSkipAction }: Props) {
  const { data: tracked, isLoading: loadingTracked } = useCourses();
  const { data: catalog, isLoading: loadingCatalog } = useCatalog();
  const { mutate: setTracked } = useSetTrackedCourses();

  const trackedIds = useMemo(
    () => new Set((tracked ?? []).map((c) => c.id)),
    [tracked],
  );

  const ordered = useMemo(() => {
    const all = catalog ?? [];
    return [...all].sort((a, b) => {
      const ac = a.code ?? "";
      const bc = b.code ?? "";
      if (ac && bc) return ac.localeCompare(bc);
      if (ac) return -1;
      if (bc) return 1;
      return a.name.localeCompare(b.name);
    });
  }, [catalog]);

  const toggle = (course: Course) => {
    const next = new Set(trackedIds);
    if (next.has(course.id)) next.delete(course.id);
    else next.add(course.id);
    setTracked(Array.from(next));
  };

  const isLoading = loadingTracked || loadingCatalog;
  const totalTracked = trackedIds.size;

  return (
    <div className="onboarding-card">
      <h1 className="onboarding-title">Which courses do you want to track?</h1>
      <p className="onboarding-blurb">
        We&apos;ve pre-selected the courses you&apos;re currently enrolled in.
        Untick any you don&apos;t want cluttering your dashboard.
      </p>

      <div className="course-grid-toolbar">
        <span className="tg-toolbar-count">
          {isLoading
            ? "Loading…"
            : `${totalTracked} of ${ordered.length} courses tracked`}
        </span>
      </div>

      {!isLoading && ordered.length === 0 ? (
        <div
          className="tg-list"
          style={{ padding: 24, textAlign: "center", color: "var(--text-muted)" }}
        >
          No courses found yet. Connect a platform to populate this list.
        </div>
      ) : null}

      {!isLoading && ordered.length > 0 ? (
        <div className="course-grid">
          {ordered.map((c) => {
            const on = trackedIds.has(c.id);
            return (
              <button
                key={c.id}
                type="button"
                className="course-card"
                data-on={on}
                onClick={() => toggle(c)}
              >
                <div className="course-card-top">
                  <span
                    className="course-card-tint"
                    style={{ background: tintFor(c.id) }}
                    aria-hidden
                  />
                  <span className="course-card-code">{c.code || "—"}</span>
                  <span className="checkbox-large checkbox-corner" aria-hidden>
                    {on ? "✓" : ""}
                  </span>
                </div>
                <div className="course-card-name">{c.name}</div>
              </button>
            );
          })}
        </div>
      ) : null}

      <div className="onboarding-actions">
        <button type="button" className="btn-ghost" onClick={onBackAction}>
          ← Back
        </button>
        <button type="button" className="btn-secondary" onClick={onSkipAction}>
          Skip for now
        </button>
        <button type="button" className="btn-primary btn-large" onClick={onContinueAction}>
          Continue →
        </button>
      </div>
    </div>
  );
}
