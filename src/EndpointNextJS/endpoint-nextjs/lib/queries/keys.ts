/**
 * Centralized query key factory. Keys are tuples — `as const` everywhere so
 * React Query's deep equality check stays cheap.
 */
export const queryKeys = {
  me: () => ["me"] as const,
  catalog: () => ["catalog"] as const,
  trackedCourses: () => ["trackedCourses"] as const,
  assignments: (params: { courseId?: string; platform?: string } = {}) =>
    ["assignments", params] as const,
  announcements: (params: { courseId?: string; platform?: string } = {}) =>
    ["announcements", params] as const,
  events: (params: { courseId?: string } = {}) => ["events", params] as const,
  readState: () => ["readState"] as const,
  connections: () => ["connections"] as const,
  preferences: () => ["preferences"] as const,
} as const;
