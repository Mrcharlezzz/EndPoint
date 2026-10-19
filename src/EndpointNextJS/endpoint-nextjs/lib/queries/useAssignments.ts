"use client";

import { useQuery } from "@tanstack/react-query";

import type { Assignment } from "@/domain/entities";
import { fetchAssignments, type AssignmentQuery } from "@/lib/api/assignments";
import { toAssignment } from "@/lib/mappers/assignment";

import { queryKeys } from "./keys";

export function useAssignments(query: AssignmentQuery = {}) {
  return useQuery<Assignment[]>({
    queryKey: queryKeys.assignments(query),
    queryFn: async () => {
      const data = await fetchAssignments(query);
      return data.results.map(toAssignment);
    },
  });
}
