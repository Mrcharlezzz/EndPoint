"use client";

import { Search, Settings } from "lucide-react";
import Link from "next/link";

import { ThemeToggle } from "@/features/preferences/ThemeToggle";
import { useSearchStore } from "@/features/search/store";
import { useSession } from "@/lib/queries/useSession";

import { Tabs } from "./Tabs";

export function TopNav() {
  const openSearch = useSearchStore((s) => s.openModal);
  const { isAuthed } = useSession();
  return (
    <header className="shell-topnav">
      <Link href="/assignments" className="shell-brand" aria-label="Endpoint home">
        <span className="shell-brand-mark" aria-hidden>
          <span />
        </span>
        <span>Endpoint</span>
      </Link>

      <Tabs />

      <span className="shell-spacer" aria-hidden />

      <button
        type="button"
        className="shell-search-trigger"
        aria-label="Quick search"
        title="Quick search (⌘K)"
        onClick={openSearch}
      >
        <Search size={14} aria-hidden />
        <span className="label">Search</span>
        <kbd className="shell-kbd">⌘K</kbd>
      </button>

      <ThemeToggle className="shell-icon-btn" />

      {isAuthed ? (
        <Link
          href="/settings"
          className="shell-icon-btn"
          aria-label="Settings"
          title="Settings"
        >
          <Settings size={16} aria-hidden />
        </Link>
      ) : (
        <Link href="/onboarding" className="shell-login-link">
          Log in
        </Link>
      )}
    </header>
  );
}
