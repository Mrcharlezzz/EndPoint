"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import {
  Database,
  Plug,
  Settings as SettingsIcon,
  SlidersHorizontal,
  User,
  X,
} from "lucide-react";

import "./settings.css";

import { AccountTab } from "./AccountTab";
import { CoursesTab } from "./CoursesTab";
import { DataTab } from "./DataTab";
import { PlatformsTab } from "./PlatformsTab";
import { PreferencesTab } from "./PreferencesTab";

type TabId = "account" | "platforms" | "courses" | "preferences" | "data";

const TABS: ReadonlyArray<{ id: TabId; label: string; icon: ReactNode }> = [
  { id: "account", label: "Account", icon: <User size={15} aria-hidden /> },
  { id: "platforms", label: "Platforms", icon: <Plug size={15} aria-hidden /> },
  { id: "courses", label: "Courses", icon: <SettingsIcon size={15} aria-hidden /> },
  { id: "preferences", label: "Preferences", icon: <SlidersHorizontal size={15} aria-hidden /> },
  { id: "data", label: "Data", icon: <Database size={15} aria-hidden /> },
];

export function SettingsPanel() {
  const router = useRouter();
  const [tab, setTab] = useState<TabId>("account");

  const close = () => router.back();

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className="settings-overlay"
      role="dialog"
      aria-modal
      aria-label="Settings"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="settings-panel">
        <aside className="settings-nav">
          <div className="settings-nav-header">
            <h2>Settings</h2>
            <button
              type="button"
              className="settings-icon-btn"
              onClick={close}
              aria-label="Close settings"
            >
              <X size={14} aria-hidden />
            </button>
          </div>
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className="settings-nav-item"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
        </aside>

        <main className="settings-content">
          {tab === "account" ? <AccountTab /> : null}
          {tab === "platforms" ? <PlatformsTab /> : null}
          {tab === "courses" ? <CoursesTab /> : null}
          {tab === "preferences" ? <PreferencesTab /> : null}
          {tab === "data" ? <DataTab /> : null}
        </main>
      </div>
    </div>
  );
}
