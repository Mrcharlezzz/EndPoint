import { apiFetch } from "./client";
import type { ApiEvent, ApiList } from "./types";

export type EventQuery = {
  courseId?: string;
};

export function fetchEvents(q: EventQuery = {}): Promise<ApiList<ApiEvent>> {
  const params = new URLSearchParams();
  if (q.courseId) params.set("course_id", q.courseId);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<ApiList<ApiEvent>>(`/api/v1/events/${suffix}`);
}
