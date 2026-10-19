"use client";

import { useEffect, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { track } from "@/features/analytics/events";
import type { ApiPreferences } from "@/lib/api/types";
import { queryKeys } from "@/lib/queries/keys";
import { usePreferences, useSetPreferences } from "@/lib/queries/usePreferences";
import { useSession } from "@/lib/queries/useSession";

const STORAGE_KEY = "endpoint-prefs";
const LEGACY_THEME_KEY = "endpoint-theme";

type ConcretePrefs = Required<
  Pick<ApiPreferences, "theme" | "density" | "accent" | "timeFmt" | "landing">
>;

const DEFAULTS: ConcretePrefs = {
  theme: "light",
  density: "standard",
  accent: "blue",
  timeFmt: "24h",
  landing: "assignments",
};

function isClient(): boolean {
  return typeof window !== "undefined";
}

function readStored(): ApiPreferences {
  if (!isClient()) return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null) return parsed;
    }
  } catch {
    /* ignore */
  }
  // Fall back to the legacy useTheme key so a returning user keeps their theme.
  try {
    const legacy = window.localStorage.getItem(LEGACY_THEME_KEY);
    if (legacy === "light" || legacy === "dark") {
      return { theme: legacy };
    }
  } catch {
    /* ignore */
  }
  return {};
}

function writeStored(prefs: ApiPreferences) {
  if (!isClient()) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* ignore */
  }
}

function resolveTheme(theme: ApiPreferences["theme"]): "light" | "dark" {
  if (theme === "dark" || theme === "light") return theme;
  if (!isClient()) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyToHtml(prefs: ConcretePrefs) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.setAttribute("data-theme", resolveTheme(prefs.theme));
  root.setAttribute("data-density", prefs.density);
  root.setAttribute("data-accent", prefs.accent);
}

export type PreferencesValue = {
  /** Effective preferences (server > local > html attrs > defaults). */
  prefs: ConcretePrefs;
  setPref: <K extends keyof ApiPreferences>(key: K, value: ApiPreferences[K]) => void;
};

/**
 * Reads persisted prefs from the API, falls back to localStorage on first
 * render (so SSR + hydration agree), applies to `<html>` attributes, and
 * writes through on mutation.
 *
 * On the login screen there is no session cookie — the server query is
 * skipped, the mutation only writes to localStorage. Once the user signs
 * in, the same code path picks up the server values.
 */
export function usePreferencesContext(): PreferencesValue {
  const { isAuthed: sessionPresent } = useSession();
  const qc = useQueryClient();
  const { data: serverPrefs } = usePreferences(sessionPresent);
  const { mutate: persist } = useSetPreferences();

  const effective = useMemo<ConcretePrefs>(() => {
    const stored = readStored();
    return {
      ...DEFAULTS,
      ...stored,
      ...(serverPrefs ?? {}),
    };
  }, [serverPrefs]);

  useEffect(() => {
    applyToHtml(effective);
    writeStored(effective);
  }, [effective]);

  const setPref = <K extends keyof ApiPreferences>(key: K, value: ApiPreferences[K]) => {
    const base = serverPrefs ?? readStored();
    const next = { ...base, [key]: value };
    // Update the React Query cache synchronously so subscribers (including
    // this hook) re-render with the new value immediately, regardless of
    // whether we then round-trip the server.
    qc.setQueryData<ApiPreferences>(queryKeys.preferences(), next);
    applyToHtml({ ...DEFAULTS, ...next });
    writeStored(next);
    if (sessionPresent) {
      persist(next);
    }
    if (value !== undefined && value !== null) {
      track("preference.changed", { key: String(key), value: value as string | number | boolean });
    }
  };

  return { prefs: effective, setPref };
}
