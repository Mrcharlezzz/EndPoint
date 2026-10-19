"use client";

import { useEffect, useRef } from "react";
import { Search, X } from "lucide-react";
import { usePathname } from "next/navigation";

import type { Item } from "@/domain/entities";
import { tintFor } from "@/domain/course";
import { PlatformIcon } from "@/features/cards/PlatformIcon";
import { useMarkRead } from "@/lib/queries/useReadState";

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

export function SearchModal() {
  const pathname = usePathname();
  const dashboard: "assignments" | "announcements" = pathname?.startsWith("/announcements")
    ? "announcements"
    : "assignments";

  const open = useSearchStore((s) => s.modalOpen);
  const query = useSearchStore((s) => s.query);
  const scope = useSearchStore((s) => s.scope);
  const setQuery = useSearchStore((s) => s.setQuery);
  const setScope = useSearchStore((s) => s.setScope);
  const closeModal = useSearchStore((s) => s.closeModal);
  const openModal = useSearchStore((s) => s.openModal);

  const inputRef = useRef<HTMLInputElement>(null);
  const results = useSearchResults(query, scope, dashboard);
  const { mutate: markReadMutate } = useMarkRead();

  // Global ⌘K / Ctrl+K toggle.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const isCmdK = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
      if (isCmdK) {
        e.preventDefault();
        if (open) closeModal();
        else openModal();
      } else if (open && e.key === "Escape") {
        closeModal();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, openModal, closeModal]);

  // Autofocus the input when the modal opens.
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="search-modal-backdrop"
      role="dialog"
      aria-modal
      aria-label="Search"
      onClick={closeModal}
    >
      <div className="search-modal" onClick={(e) => e.stopPropagation()}>
        <div className="search-modal-input-wrap">
          <Search size={16} aria-hidden />
          <input
            ref={inputRef}
            type="search"
            className="search-modal-input"
            placeholder="Search across your courses…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search"
          />
          <span className="search-panel-kbd">esc</span>
          <button
            type="button"
            className="shell-icon-btn"
            onClick={closeModal}
            aria-label="Close"
          >
            <X size={14} aria-hidden />
          </button>
        </div>

        <div className="search-panel-scope" style={{ padding: "0 16px 12px" }}>
          <button
            type="button"
            className="search-scope-btn"
            data-active={scope === "current"}
            onClick={() => setScope("current")}
          >
            Current dashboard
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

        <div className="search-modal-results">
          {!query.trim() ? (
            <p className="search-panel-empty">Start typing to search.</p>
          ) : null}

          {query.trim() && results.total === 0 ? (
            <p className="search-panel-empty">
              No matches for &ldquo;{query}&rdquo;
            </p>
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
                {items.slice(0, 12).map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="search-panel-result"
                    onClick={() => {
                      markReadMutate([item.id]);
                      if (item.sourceUrl) {
                        window.open(item.sourceUrl, "_blank", "noopener,noreferrer");
                      }
                      closeModal();
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
      </div>
    </div>
  );
}
