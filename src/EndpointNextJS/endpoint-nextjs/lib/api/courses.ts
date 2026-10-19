import { apiFetch } from "./client";
import type { ApiCourse, ApiList, ApiUserCourse } from "./types";

export function fetchCatalog(): Promise<ApiList<ApiCourse>> {
  return apiFetch<ApiList<ApiCourse>>("/api/v1/courses/");
}

export function fetchTrackedCourses(): Promise<ApiList<ApiUserCourse>> {
  return apiFetch<ApiList<ApiUserCourse>>("/api/v1/users/me/courses/");
}

export function setTrackedCourses(courseIds: string[]): Promise<ApiList<ApiUserCourse>> {
  return apiFetch<ApiList<ApiUserCourse>>("/api/v1/users/me/courses/", {
    method: "PUT",
    body: { course_ids: courseIds },
  });
}
