"use client";

import { create } from "zustand";

import type { Item, Platform } from "@/domain/entities";

type State = {
  /** Selected course ids; empty set means "all". */
  courses: ReadonlySet<string>;
  /** Single-select platform; null = all. */
  platform: Platform | null;
  /** Single-select type (announcements dashboard only); null = all. */
  type: Item["type"] | null;

  setPlatform: (p: Platform | null) => void;
  setType: (t: Item["type"] | null) => void;
  toggleCourse: (id: string) => void;
  setCourses: (ids: string[]) => void;
  clear: () => void;
};

export const useFiltersStore = create<State>((set) => ({
  courses: new Set<string>(),
  platform: null,
  type: null,

  setPlatform: (platform) => set({ platform }),
  setType: (type) => set({ type }),
  toggleCourse: (id) =>
    set((state) => {
      const next = new Set(state.courses);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return { courses: next };
    }),
  setCourses: (ids) => set({ courses: new Set(ids) }),
  clear: () => set({ courses: new Set<string>(), platform: null, type: null }),
}));

export function selectHasFilters(s: State): boolean {
  return s.courses.size > 0 || s.platform !== null || s.type !== null;
}
