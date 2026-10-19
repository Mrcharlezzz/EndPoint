"use client";

import { Moon, Sun } from "lucide-react";

import { usePreferencesContext } from "./PreferencesContext";

type Props = {
  className?: string;
};

export function ThemeToggle({ className = "theme-toggle" }: Props) {
  const { prefs, setPref } = usePreferencesContext();
  const resolved = prefs.theme === "dark" ? "dark" : prefs.theme === "light" ? "light" : null;
  // When theme is "system" we still want the toggle to flip relative to the
  // currently-displayed theme — read the html attribute so the icon always
  // matches the surface.
  const current: "light" | "dark" =
    resolved ??
    (typeof document !== "undefined" && document.documentElement.dataset.theme === "dark"
      ? "dark"
      : "light");
  const next: "light" | "dark" = current === "light" ? "dark" : "light";

  return (
    <button
      type="button"
      className={className}
      onClick={() => setPref("theme", next)}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      suppressHydrationWarning
    >
      <span suppressHydrationWarning style={{ display: "inline-flex" }}>
        {current === "light" ? <Moon size={16} aria-hidden /> : <Sun size={16} aria-hidden />}
      </span>
    </button>
  );
}
