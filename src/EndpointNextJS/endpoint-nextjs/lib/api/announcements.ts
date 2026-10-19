import { apiFetch } from "./client";
import type { ApiAnnouncementGroup, ApiList } from "./types";

export type AnnouncementQuery = {
  courseId?: string;
  platform?: string;
};

export function fetchAnnouncements(
  q: AnnouncementQuery = {},
): Promise<ApiList<ApiAnnouncementGroup>> {
  const params = new URLSearchParams();
  if (q.courseId) params.set("course_id", q.courseId);
  if (q.platform) params.set("platform", q.platform);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<ApiList<ApiAnnouncementGroup>>(`/api/v1/announcements/${suffix}`);
}
