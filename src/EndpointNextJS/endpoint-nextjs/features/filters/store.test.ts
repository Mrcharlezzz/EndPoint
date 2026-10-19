import { beforeEach, describe, expect, it } from "vitest";

import { useFiltersStore } from "./store";

describe("useFiltersStore — courses", () => {
  beforeEach(() => {
    useFiltersStore.getState().clear();
  });

  it("toggleCourse adds then removes a course id", () => {
    useFiltersStore.getState().toggleCourse("course-1");
    expect(useFiltersStore.getState().courses.has("course-1")).toBe(true);

    useFiltersStore.getState().toggleCourse("course-1");
    expect(useFiltersStore.getState().courses.has("course-1")).toBe(false);
  });

  it("setCourses replaces the selection", () => {
    useFiltersStore.getState().setCourses(["a", "b"]);
    expect(Array.from(useFiltersStore.getState().courses).sort()).toEqual(["a", "b"]);
  });
});
