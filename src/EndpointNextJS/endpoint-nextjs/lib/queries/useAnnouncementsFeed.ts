"use client";

import { useQueries } from "@tanstack/react-query";

import type { Announcement, Event } from "@/domain/entities";
import { fetchAnnouncements, type AnnouncementQuery } from "@/lib/api/announcements";
import { fetchEvents, type EventQuery } from "@/lib/api/events";
import { toAnnouncementsFlat } from "@/lib/mappers/announcement";
import { toEvent } from "@/lib/mappers/event";

import { queryKeys } from "./keys";

export type AnnouncementsFeedItem = Announcement | Event;

type Result = {
  data: AnnouncementsFeedItem[] | undefined;
  isLoading: boolean;
  isError: boolean;
};

/**
 * The Announcements dashboard merges announcements + events into a single
 * feed sorted by published date. Two queries fan out, results merge once
 * both resolve. React Query handles dedupe + caching per key.
 */
export function useAnnouncementsFeed(
  filters: AnnouncementQuery & EventQuery = {},
): Result {
  const queries = useQueries({
    queries: [
      {
        queryKey: queryKeys.announcements(filters),
        queryFn: async () => {
          const data = await fetchAnnouncements(filters);
          return toAnnouncementsFlat(data.results);
        },
      },
      {
        queryKey: queryKeys.events({ courseId: filters.courseId }),
        queryFn: async () => {
          const data = await fetchEvents({ courseId: filters.courseId });
          return data.results.map(toEvent);
        },
      },
    ],
  });

  const [annQuery, evQuery] = queries;
  const isLoading = annQuery!.isLoading || evQuery!.isLoading;
  const isError = annQuery!.isError || evQuery!.isError;

  let data: AnnouncementsFeedItem[] | undefined;
  if (annQuery!.data && evQuery!.data) {
    data = [...annQuery!.data, ...evQuery!.data].sort(
      (a, b) => new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime(),
    );
  }

  return { data, isLoading, isError };
}
