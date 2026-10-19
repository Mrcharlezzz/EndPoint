"use client";

import { useMemo } from "react";

import type { Item } from "@/domain/entities";
import { useAssignments } from "@/lib/queries/useAssignments";
import { useAnnouncementsFeed } from "@/lib/queries/useAnnouncementsFeed";

export type SearchResults = {
  query: string;
  byType: { assignments: Item[]; announcements: Item[]; events: Item[] };
  total: number;
};

const EMPTY: SearchResults = {
  query: "",
  byType: { assignments: [], announcements: [], events: [] },
  total: 0,
};

/**
 * Client-side search over the cards already cached by React Query.
 * Phase 7's MVP — swap to a server endpoint by replacing this hook.
 */
export function useSearchResults(query: string, scope: "current" | "all", currentDashboard: "assignments" | "announcements"): SearchResults {
  const { data: assignments } = useAssignments();
  const { data: announcementsFeed } = useAnnouncementsFeed();

  return useMemo<SearchResults>(() => {
    const q = query.trim().toLowerCase();
    if (!q) return EMPTY;

    const allItems: Item[] = [];
    if (scope === "all" || currentDashboard === "assignments") {
      allItems.push(...(assignments ?? []));
    }
    if (scope === "all" || currentDashboard === "announcements") {
      allItems.push(...(announcementsFeed ?? []));
    }

    const matches = allItems.filter((item) => {
      const title = (item.title || "").toLowerCase();
      const body = (item.body || "").toLowerCase();
      return title.includes(q) || body.includes(q);
    });

    return {
      query,
      byType: {
        assignments: matches.filter((i) => i.type === "assignment"),
        announcements: matches.filter((i) => i.type === "announcement"),
        events: matches.filter((i) => i.type === "event"),
      },
      total: matches.length,
    };
  }, [query, scope, currentDashboard, assignments, announcementsFeed]);
}
