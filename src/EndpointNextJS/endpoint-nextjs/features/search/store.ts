"use client";

import { create } from "zustand";

type Scope = "current" | "all";

type State = {
  query: string;
  scope: Scope;
  modalOpen: boolean;
  setQuery: (q: string) => void;
  setScope: (s: Scope) => void;
  openModal: () => void;
  closeModal: () => void;
};

export const useSearchStore = create<State>((set) => ({
  query: "",
  scope: "current",
  modalOpen: false,
  setQuery: (query) => set({ query }),
  setScope: (scope) => set({ scope }),
  openModal: () => set({ modalOpen: true }),
  closeModal: () => set({ modalOpen: false }),
}));
