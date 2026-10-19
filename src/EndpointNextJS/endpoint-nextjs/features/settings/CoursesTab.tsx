"use client";

import { useMemo, useState } from "react";
import { Library, Search, X } from "lucide-react";

import type { Course } from "@/domain/course";
import { tintFor } from "@/domain/course";
import {
  useCatalog,
  useCourses,
  useSetTrackedCourses,
} from "@/lib/queries/useCourses";

/**
 * Pulls the leading letters out of a course code for the avatar glyph.
 * "CS-210" → "CS", "MATH-220" → "MAT". Falls back to the first two letters
 * of the course name when no code is set.
 */
function avatarLabel(c: Course): string {
  if (c.code) {
    const m = c.code.match(/^[A-Za-z]+/);
    if (m) return m[0].slice(0, 3).toUpperCase();
    return c.code.slice(0, 2).toUpperCase();
  }
  return c.name.slice(0, 2).toUpperCase();
}

function matchesQuery(c: Course, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return (
    c.name.toLowerCase().includes(needle) ||
    (c.code ?? "").toLowerCase().includes(needle) ||
    (c.major ?? "").toLowerCase().includes(needle) ||
    (c.semester ?? "").toLowerCase().includes(needle)
  );
}

export function CoursesTab() {
  const { data: tracked, isLoading: loadingTracked } = useCourses();
  const { data: catalog, isLoading: loadingCatalog } = useCatalog();
  const { mutate: setTracked } = useSetTrackedCourses();
  const [query, setQuery] = useState("");

  const trackedIds = useMemo(
    () => new Set((tracked ?? []).map((c) => c.id)),
    [tracked],
  );

  const ordered = useMemo(() => {
    const all = catalog ?? [];
    return [...all].sort((a, b) => {
      const ac = a.code ?? "";
      const bc = b.code ?? "";
      if (ac && bc) return ac.localeCompare(bc, undefined, { numeric: true });
      if (ac) return -1;
      if (bc) return 1;
      return a.name.localeCompare(b.name);
    });
  }, [catalog]);

  const visible = useMemo(
    () => ordered.filter((c) => matchesQuery(c, query.trim())),
    [ordered, query],
  );

  const totalTracked = trackedIds.size;
  const totalCatalog = ordered.length;
  const visibleTrackedCount = visible.filter((c) => trackedIds.has(c.id)).length;
  const allVisibleTracked = visible.length > 0 && visibleTrackedCount === visible.length;

  const toggle = (course: Course) => {
    const next = new Set(trackedIds);
    if (next.has(course.id)) next.delete(course.id);
    else next.add(course.id);
    setTracked(Array.from(next));
  };

  const trackAllVisible = () => {
    const next = new Set(trackedIds);
    visible.forEach((c) => next.add(c.id));
    setTracked(Array.from(next));
  };

  const untrackAllVisible = () => {
    const next = new Set(trackedIds);
    visible.forEach((c) => next.delete(c.id));
    setTracked(Array.from(next));
  };

  const isLoading = loadingTracked || loadingCatalog;
  const hasCatalog = ordered.length > 0;
  const queryActive = query.trim().length > 0;

  return (
    <>
      <h3 className="settings-section-title">Courses</h3>
      <p className="settings-section-sub">
        Pick the courses to surface on your dashboards.
      </p>

      {hasCatalog ? (
        <div className="course-tab-toolbar">
          <div className="settings-search">
            <Search className="settings-search-icon" size={14} aria-hidden />
            <input
              type="search"
              className="settings-search-input"
              placeholder="Search courses…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search courses"
            />
            {query ? (
              <button
                type="button"
                className="settings-search-clear"
                onClick={() => setQuery("")}
                aria-label="Clear search"
              >
                <X size={12} aria-hidden />
              </button>
            ) : null}
          </div>

          <div className="course-tab-summary">
            <span>
              {totalTracked} of {totalCatalog} tracked
            </span>
          </div>

          {visible.length > 0 ? (
            allVisibleTracked ? (
              <button type="button" className="btn-text btn-sm" onClick={untrackAllVisible}>
                {queryActive ? "Untrack visible" : "Untrack all"}
              </button>
            ) : (
              <button type="button" className="btn-text btn-sm" onClick={trackAllVisible}>
                {queryActive ? "Track visible" : "Track all"}
              </button>
            )
          ) : null}
        </div>
      ) : null}

      {isLoading ? (
        <div className="course-tab-empty">
          <span style={{ fontSize: 13 }}>Loading courses…</span>
        </div>
      ) : null}

      {!isLoading && !hasCatalog ? (
        <div className="course-tab-empty">
          <span className="course-tab-empty-icon" aria-hidden>
            <Library size={18} />
          </span>
          <span className="course-tab-empty-title">No courses yet</span>
          <span className="course-tab-empty-body">
            Connect Moodle or Teams from the Platforms tab and Endpoint will
            populate the catalog automatically.
          </span>
        </div>
      ) : null}

      {!isLoading && hasCatalog && visible.length === 0 ? (
        <div className="course-tab-empty">
          <span className="course-tab-empty-title">No matches</span>
          <span className="course-tab-empty-body">
            No courses match &ldquo;{query.trim()}&rdquo;. Try a different
            search.
          </span>
        </div>
      ) : null}

      {!isLoading && visible.length > 0 ? (
        <div className="course-tab-list">
          {visible.map((c) => {
            const on = trackedIds.has(c.id);
            return (
              <button
                key={c.id}
                type="button"
                className="course-tab-row"
                data-on={on}
                onClick={() => toggle(c)}
                aria-pressed={on}
                aria-label={`${on ? "Untrack" : "Track"} ${c.name}`}
              >
                <span
                  className="course-tab-avatar"
                  style={{ background: tintFor(c.id) }}
                  aria-hidden
                >
                  {avatarLabel(c)}
                </span>
                <span className="course-tab-info">
                  <span className="course-tab-name">{c.name}</span>
                  <span className="course-tab-meta">
                    <span className="course-tab-code">{c.code || "—"}</span>
                    {c.semester ? (
                      <>
                        <span className="course-tab-meta-divider" aria-hidden>·</span>
                        <span>{c.semester}</span>
                      </>
                    ) : null}
                    {c.major ? (
                      <>
                        <span className="course-tab-meta-divider" aria-hidden>·</span>
                        <span>{c.major}</span>
                      </>
                    ) : null}
                  </span>
                </span>
                <span
                  className="toggle"
                  data-on={on}
                  aria-hidden
                />
              </button>
            );
          })}
        </div>
      ) : null}
    </>
  );
}
