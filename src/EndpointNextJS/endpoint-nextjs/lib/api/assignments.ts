import { apiFetch } from "./client";
import type { ApiAssignment, ApiList } from "./types";

export type AssignmentQuery = {
  courseId?: string;
  platform?: string;
};

export function fetchAssignments(q: AssignmentQuery = {}): Promise<ApiList<ApiAssignment>> {
  const params = new URLSearchParams();
  if (q.courseId) params.set("course_id", q.courseId);
  if (q.platform) params.set("platform", q.platform);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<ApiList<ApiAssignment>>(`/api/v1/assignments/${suffix}`);
}

export function fetchAssignment(id: string): Promise<ApiAssignment> {
  return apiFetch<ApiAssignment>(`/api/v1/assignments/${id}/`);
}
