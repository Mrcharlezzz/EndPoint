import type { ReactNode } from "react";

import "./shell.css";

import { ResultBar } from "@/features/dashboard/ResultBar";
import { FilterSidebar } from "@/features/filters/FilterSidebar";
import { FirstRunGate } from "@/features/onboarding/FirstRunGate";
import { PreferencesProvider } from "@/features/preferences/PreferencesProvider";
import { SearchModal } from "@/features/search/SearchModal";
import { SearchPanel } from "@/features/search/SearchPanel";

import { TopNav } from "./TopNav";

type Props = {
  children: ReactNode;
};

export function AppShell({ children }: Props) {
  return (
    <PreferencesProvider>
      <div className="shell-root">
        <TopNav />
        <ResultBar />

        <div className="shell-layout">
          <div className="shell-sidebar-col">
            <FilterSidebar />
          </div>
          <div className="shell-content">{children}</div>
          <div className="shell-search-panel-col">
            <SearchPanel />
          </div>
        </div>

        <SearchModal />
        <FirstRunGate />
      </div>
    </PreferencesProvider>
  );
}
