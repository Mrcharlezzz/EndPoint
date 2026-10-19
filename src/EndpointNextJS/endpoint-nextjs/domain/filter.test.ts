import { describe, expect, it } from "vitest";

import { hasActiveFilters, matchesFilters, noFilters } from "./filter";
import type { Assignment } from "./entities";

function assignment(courseId: string, overrides: Partial<Assignment> = {}): Assignment {
  return {
    type: "assignment",
    id: "item-1",
    title: "Lab",
    body: null,
    author: "TA",
    course: {
      id: courseId,
      code: "CS",
      name: "Course",
      major: null,
      semester: null,
    },
    platform: "moodle",
    sourceUrl: null,
    publishedAt: "2026-01-01T00:00:00.000Z",
    links: [],
    attachments: [],
    deadline: null,
    submissionFormat: "file_upload",
    ...overrides,
  };
}

describe("matchesFilters — courses", () => {
  const base = noFilters();

  it("does not filter by course when the course set is empty (all courses)", () => {
    const item = assignment("course-a");
    expect(matchesFilters(item, { ...base, courses: new Set() })).toBe(true);
  });

  it("keeps items whose course id is in the selection", () => {
    const item = assignment("course-a");
    expect(
      matchesFilters(item, {
        ...base,
        courses: new Set(["course-a"]),
      }),
    ).toBe(true);
  });

  it("removes items whose course id is not in the selection", () => {
    const item = assignment("course-b");
    expect(
      matchesFilters(item, {
        ...base,
        courses: new Set(["course-a"]),
      }),
    ).toBe(false);
  });

  it("allows an item when any of several course ids match (multi-select)", () => {
    const item = assignment("course-b");
    expect(
      matchesFilters(item, {
        ...base,
        courses: new Set(["course-a", "course-b"]),
      }),
    ).toBe(true);
  });
});

describe("hasActiveFilters", () => {
  it("is false when no course, platform, or type is set", () => {
    expect(hasActiveFilters(noFilters())).toBe(false);
  });

  it("is true when at least one course is selected", () => {
    expect(
      hasActiveFilters({
        courses: new Set(["x"]),
        platform: null,
        type: null,
      }),
    ).toBe(true);
  });
});
