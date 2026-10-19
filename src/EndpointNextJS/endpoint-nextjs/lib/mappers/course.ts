import type { Course } from "@/domain/course";

import type { ApiCourse, ApiUserCourse } from "@/lib/api/types";

/**
 * The catalog endpoint returns the full course shape; the announcement /
 * assignment endpoints embed a trimmed `{id, name, code}` only. Both go
 * through this mapper so the UI receives a consistent `Course` regardless
 * of where it came from.
 */
export function toCourse(api: ApiCourse | ApiUserCourse | { id: string; code: string; name: string }): Course {
  const id = "id" in api ? api.id : (api as ApiUserCourse).course_id;
  return {
    id,
    code: api.code,
    name: api.name,
    major: "major" in api ? (api.major ?? null) : null,
    semester: "semester" in api ? (api.semester ?? null) : null,
  };
}
