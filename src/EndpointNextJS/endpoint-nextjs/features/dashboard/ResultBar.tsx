"use client";

import { Suspense, useMemo } from "react";
import { Check } from "lucide-react";

import { selectHasFilters, useFiltersStore } from "@/features/filters/store";
import { useFilterUrlSync } from "@/features/filters/urlSync";
import { useMarkRead, useReadState } from "@/lib/queries/useReadState";
import { useSession } from "@/lib/queries/useSession";

import { useVisibleStore } from "./visibleStore";

/**
 * The bar lives between the top nav and the layout. It owns:
 * - the result count (filtered items in view)
 * - the "Mark all as read" button (over visible unread)
 * - the URL ↔ filter store sync
 */
function ResultBarInner() {
  useFilterUrlSync();
  const visibleIds = useVisibleStore((s) => s.visibleIds);
  const hasFilters = useFiltersStore(selectHasFilters);
  const { isAuthed } = useSession();
  const { data: readMap } = useReadState({ enabled: isAuthed });
  const { mutate: markReadMutate } = useMarkRead();

  const unreadIds = useMemo(() => {
    if (!readMap) return visibleIds;
    return visibleIds.filter((id) => !readMap[id]);
  }, [visibleIds, readMap]);

  const onMarkAll = () => {
    if (unreadIds.length === 0) return;
    markReadMutate(Array.from(unreadIds));
  };

  return (
    <div className="shell-result-bar">
      <span className="shell-result-count">
        {visibleIds.length} {visibleIds.length === 1 ? "item" : "items"}
        {hasFilters ? " · filtered" : ""}
      </span>
      {isAuthed ? (
        <button
          type="button"
          className="shell-mark-all-btn"
          onClick={onMarkAll}
          disabled={unreadIds.length === 0}
        >
          <Check size={13} aria-hidden />
          Mark all as read
        </button>
      ) : null}
    </div>
  );
}

export function ResultBar() {
  // useFilterUrlSync uses useSearchParams which requires a Suspense
  // boundary on Next 16.
  return (
    <Suspense fallback={<div className="shell-result-bar" aria-hidden />}>
      <ResultBarInner />
    </Suspense>
  );
}
