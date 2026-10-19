"use client";

import { create } from "zustand";

/**
 * The shell's result bar needs to know what's currently on screen so it
 * can show a count and run "Mark all visible as read". Each dashboard
 * view writes its filtered item ids here once on render.
 */
type State = {
  visibleIds: ReadonlyArray<string>;
  setVisibleIds: (ids: string[]) => void;
};

export const useVisibleStore = create<State>((set) => ({
  visibleIds: [],
  setVisibleIds: (ids) => set({ visibleIds: ids }),
}));
