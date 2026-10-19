"use client";

import { type ReactNode } from "react";

import { usePreferencesContext } from "./PreferencesContext";

/**
 * Mounts {@link usePreferencesContext} once near the top of the app shell
 * so its <html> attribute write happens on first paint, not lazily when
 * the Preferences tab opens. Renders nothing of its own.
 */
export function PreferencesProvider({ children }: { children: ReactNode }) {
  // The hook's effects run on mount: read localStorage + apply <html> attrs,
  // then keep them in sync as React Query resolves the server prefs.
  usePreferencesContext();
  return <>{children}</>;
}
