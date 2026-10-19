"use client";

import { Search, X } from "lucide-react";
import { usePathname } from "next/navigation";

import type { Item } from "@/domain/entities";
import { tintFor } from "@/domain/course";
import { PlatformIcon } from "@/features/cards/PlatformIcon";
import { useMarkRead } from "@/lib/queries/useReadState";
import { useSession } from "@/lib/queries/useSession";

import { useSearchStore } from "./store";
import { useSearchResults } from "./useSearchResults";

const TYPE_LABEL: Record<Item["type"], string> = {
  assignment: "Assignments",
  announcement: "Announcements",
  event: "Events",
};

function highlight(text: string, query: string) {
  if (!query || !text) return text;
  const lower = text.toLowerCase();
  const idx = lower.indexOf(query.toLowerCase());
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark>{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  );
}

export function SearchPanel() {
  const pathname = usePathname();
  const dashboard: "assignments" | "announcements" = pathname?.startsWith("/announcements")
    ? "announcements"
    : "assignments";

  const query = useSearchStore((s) => s.query);
  const scope = useSearchStore((s) => s.scope);
  const setQuery = useSearchStore((s) => s.setQuery);
  const setScope = useSearchStore((s) => s.setScope);

  const results = useSearchResults(query, scope, dashboard);
  const { isAuthed } = useSession();
  const { mutate: markReadMutate } = useMarkRead();

  return (
    <aside className="shell-search-panel">
      <div className="search-panel-header">
        <Search size={14} aria-hidden />
        <span className="search-panel-title">Search</span>
        <span className="search-panel-kbd">⌘K</span>
      </div>

      <div className="search-panel-input-wrap">
        <input
          type="search"
          className="search-panel-input"
          placeholder="Search titles, bodies…"
          aria-label="Search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query ? (
          <button
            type="button"
            className="shell-icon-btn"
            onClick={() => setQuery("")}
            aria-label="Clear search"
          >
            <X size={14} aria-hidden />
          </button>
        ) : null}
      </div>

      <div className="search-panel-scope">
        <button
          type="button"
          className="search-scope-btn"
          data-active={scope === "current"}
          onClick={() => setScope("current")}
        >
          Current
        </button>
        <button
          type="button"
          className="search-scope-btn"
          data-active={scope === "all"}
          onClick={() => setScope("all")}
        >
          All
        </button>
        {query ? (
          <span className="search-panel-count">
            {results.total} {results.total === 1 ? "result" : "results"}
          </span>
        ) : null}
      </div>

      <div className="search-panel-results">
        {!query.trim() ? (
          <p className="search-panel-empty">
            Find anything across your courses, fast.
          </p>
        ) : null}

        {query.trim() && results.total === 0 ? (
          <p className="search-panel-empty">No matches for &ldquo;{query}&rdquo;</p>
        ) : null}

        {(["assignment", "announcement", "event"] as const).map((type) => {
          const items = (results.byType as Record<string, Item[]>)[`${type}s`] ?? [];
          if (items.length === 0) return null;
          return (
            <div key={type}>
              <div className="search-group-label">
                {TYPE_LABEL[type]}
                <span className="search-group-count">{items.length}</span>
              </div>
              {items.slice(0, 8).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="search-panel-result"
                  onClick={() => {
                    if (isAuthed) {
                      markReadMutate([item.id]);
                    }
                    if (item.sourceUrl) {
                      window.open(item.sourceUrl, "_blank", "noopener,noreferrer");
                    }
                  }}
                >
                  <div className="search-result-overline">
                    <span
                      style={{
                        width: 6,
                        height: 6,
                        borderRadius: 999,
                        background: tintFor(item.course.id),
                      }}
                      aria-hidden
                    />
                    <span>{item.course.code || item.course.name}</span>
                    <PlatformIcon platform={item.platform} size={11} />
                  </div>
                  <div className="search-result-title">
                    {highlight(item.title || `(untitled ${item.type})`, query)}
                  </div>
                  {item.body ? (
                    <div className="search-result-snippet">
                      {highlight(item.body, query)}
                    </div>
                  ) : null}
                </button>
              ))}
            </div>
          );
        })}
      </div>
    </aside>
  );
}
