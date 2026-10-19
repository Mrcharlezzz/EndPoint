import type { Assignment, Item } from "./entities";

/** Closest deadline first; missing deadlines sink to the bottom. */
export function byDeadlineAsc(a: Assignment, b: Assignment): number {
  if (!a.deadline && !b.deadline) return 0;
  if (!a.deadline) return 1;
  if (!b.deadline) return -1;
  return new Date(a.deadline).getTime() - new Date(b.deadline).getTime();
}

function assignmentRecentTime(a: Assignment): number {
  const primary = a.deadline ?? a.publishedAt;
  const time = new Date(primary).getTime();
  return Number.isNaN(time) ? 0 : time;
}

/** Newest first by deadline when available, otherwise published date. */
export function byAssignmentRecentDesc(a: Assignment, b: Assignment): number {
  return assignmentRecentTime(b) - assignmentRecentTime(a);
}

/** Newest first by published date — used for the announcements feed. */
export function byPublishedDesc(a: Item, b: Item): number {
  return new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime();
}
