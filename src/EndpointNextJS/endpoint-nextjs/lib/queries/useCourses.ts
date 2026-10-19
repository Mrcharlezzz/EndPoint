"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Course } from "@/domain/course";
import { fetchCatalog, fetchTrackedCourses, setTrackedCourses } from "@/lib/api/courses";
import { toCourse } from "@/lib/mappers/course";

import { queryKeys } from "./keys";

type QueryOptions = {
  enabled?: boolean;
};

export function useCourses({ enabled = true }: QueryOptions = {}) {
  return useQuery<Course[]>({
    queryKey: queryKeys.trackedCourses(),
    queryFn: async () => {
      const data = await fetchTrackedCourses();
      return data.results.map(toCourse);
    },
    enabled,
  });
}

export function useCatalog() {
  return useQuery<Course[]>({
    queryKey: queryKeys.catalog(),
    queryFn: async () => {
      const data = await fetchCatalog();
      return data.results.map(toCourse);
    },
  });
}

export function useSetTrackedCourses() {
  const qc = useQueryClient();
  return useMutation<Course[], unknown, string[]>({
    mutationFn: async (ids) => {
      const data = await setTrackedCourses(ids);
      return data.results.map(toCourse);
    },
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.trackedCourses(), data);
    },
    meta: { errorToast: "Couldn't update your course selection." },
  });
}
