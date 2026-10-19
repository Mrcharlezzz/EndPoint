"use client";

import { useMemo } from "react";
import { usePathname } from "next/navigation";

import type { Item, Platform } from "@/domain/entities";
import { tintFor } from "@/domain/course";
import { PlatformIcon } from "@/features/cards/PlatformIcon";
import { useAnnouncementsFeed } from "@/lib/queries/useAnnouncementsFeed";
import { useAssignments } from "@/lib/queries/useAssignments";

import { selectHasFilters, useFiltersStore } from "./store";

const PLATFORM_OPTIONS: { id: Platform | "all"; label: string }[] = [
  { id: "all", label: "All platforms" },
  { id: "moodle", label: "Moodle" },
  { id: "teams", label: "Teams" },
  { id: "telegram", label: "Telegram" },
];

const TYPE_OPTIONS: { id: Item["type"] | "all"; label: string }[] = [
  { id: "all", label: "All" },
  { id: "announcement", label: "Announcements" },
  { id: "event", label: "Events" },
];

export function FilterSidebar() {
  const pathname = usePathname();
  const showTypeFilter = pathname?.startsWith("/announcements") ?? false;

  const platform = useFiltersStore((s) => s.platform);
  const type = useFiltersStore((s) => s.type);
  const courses = useFiltersStore((s) => s.courses);
  const setPlatform = useFiltersStore((s) => s.setPlatform);
  const setType = useFiltersStore((s) => s.setType);
  const toggleCourse = useFiltersStore((s) => s.toggleCourse);
  const clear = useFiltersStore((s) => s.clear);
  const hasActive = useFiltersStore(selectHasFilters);

  const { data: assignments } = useAssignments();
  const { data: announcementsFeed } = useAnnouncementsFeed();

  const availableCourses = useMemo(() => {
    const byId = new Map<string, { id: string; name: string; code: string }>();
    for (const item of [...(assignments ?? []), ...(announcementsFeed ?? [])]) {
      byId.set(item.course.id, item.course);
    }
    return Array.from(byId.values()).sort((a, b) => a.name.localeCompare(b.name));
  }, [assignments, announcementsFeed]);

  return (
    <aside className="shell-sidebar">
      <div className="sidebar-header">
        <span className="sidebar-title">Filters</span>
        <button
          type="button"
          className="clear-link"
          onClick={clear}
          aria-hidden={!hasActive}
          tabIndex={hasActive ? 0 : -1}
          style={{ visibility: hasActive ? "visible" : "hidden" }}
        >
          Clear
        </button>
      </div>

      <div className="sidebar-section">
        <div className="sidebar-section-label">Platform</div>
        <div className="seg-vert">
          {PLATFORM_OPTIONS.map((o) => {
            const active = o.id === "all" ? platform === null : platform === o.id;
            const isChild = o.id !== "all";
            return (
              <button
                key={o.id}
                type="button"
                className={
                  isChild
                    ? "seg-vert-item seg-vert-item--child"
                    : "seg-vert-item"
                }
                data-on={active}
                onClick={() =>
                  setPlatform(o.id === "all" ? null : (o.id as Platform))
                }
              >
                {isChild ? (
                  <PlatformIcon platform={o.id as Platform} size={13} />
                ) : null}
                <span>{o.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {showTypeFilter ? (
        <div className="sidebar-section">
          <div className="sidebar-section-label">Type</div>
          <div className="seg-vert">
            {TYPE_OPTIONS.map((o) => {
              const active = o.id === "all" ? type === null : type === o.id;
              return (
                <button
                  key={o.id}
                  type="button"
                  className="seg-vert-item"
                  data-on={active}
                  onClick={() =>
                    setType(o.id === "all" ? null : (o.id as Item["type"]))
                  }
                >
                  <span>{o.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="sidebar-section">
        <div className="sidebar-section-label">
          Courses
          {courses.size > 0 ? (
            <span className="sidebar-count">{courses.size}</span>
          ) : null}
        </div>
        <div className="course-list">
          {availableCourses.map((c) => (
            <label
              key={c.id}
              className="course-row"
              data-checked={courses.has(c.id)}
            >
              <input
                type="checkbox"
                checked={courses.has(c.id)}
                onChange={() => toggleCourse(c.id)}
              />
              <span
                className="course-swatch"
                style={{ background: tintFor(c.id) }}
                aria-hidden
              />
              <span className="course-row-name">{c.name}</span>
              {c.code ? <span className="course-row-code">{c.code}</span> : null}
            </label>
          ))}
          {availableCourses.length === 0 ? (
            <p className="search-panel-empty" style={{ padding: "8px 4px" }}>
              No courses available.
            </p>
          ) : null}
        </div>
      </div>
    </aside>
  );
}
