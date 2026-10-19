import { deadlineUrgency } from "./deadline";
import type { Assignment, Item, Platform } from "./entities";

export type ItemFilters = {
  /** Selected course ids; empty set means "all courses". */
  courses: ReadonlySet<string>;
  /** Selected platform; null means "all platforms". */
  platform: Platform | null;
  /** For announcements feed: filter by entity type; null = all. */
  type: Item["type"] | null;
};

export function noFilters(): ItemFilters {
  return { courses: new Set(), platform: null, type: null };
}

export function hasActiveFilters(f: ItemFilters): boolean {
  return f.courses.size > 0 || f.platform !== null || f.type !== null;
}

export function matchesFilters(item: Item, f: ItemFilters): boolean {
  if (f.courses.size > 0 && !f.courses.has(item.course.id)) return false;
  if (f.platform && item.platform !== f.platform) return false;
  if (f.type && item.type !== f.type) return false;
  return true;
}

export function isOverdue(a: Assignment, now: Date = new Date()): boolean {
  return deadlineUrgency(a.deadline, now) === "overdue";
}

export function hasUpcomingDeadline(a: Assignment, now: Date = new Date()): boolean {
  if (!a.deadline) return false;
  const deadline = new Date(a.deadline).getTime();
  if (Number.isNaN(deadline)) return false;
  return deadline >= now.getTime();
}
