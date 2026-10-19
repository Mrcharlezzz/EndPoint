"use client";

import { useEffect, type ReactNode } from "react";
import { usePathname } from "next/navigation";

import { useMe } from "@/lib/queries/useMe";

import { getPostHog, loadPostHog } from "./client";

/**
 * Boots PostHog once on the client, identifies the signed-in user by id
 * (no PII), and emits a $pageview on each pathname change. Mounted near
 * the root of the tree from `app/providers.tsx`.
 *
 * Identification by id only: emails, display names, and any other
 * profile fields stay out of PostHog. The id is enough to count unique
 * users, build funnels, and slice retention.
 */
export function PostHogProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { data: user } = useMe();
  const userId = user?.id ?? null;

  // Lazy-load posthog-js once on first mount.
  useEffect(() => {
    void loadPostHog();
  }, []);

  // Identify on auth, reset on logout.
  useEffect(() => {
    let cancelled = false;
    void loadPostHog().then((posthog) => {
      if (cancelled || !posthog) return;
      if (userId) {
        posthog.identify(userId);
      } else {
        posthog.reset();
      }
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // Emit a pageview on every pathname change. Search params are excluded
  // on purpose — filter changes shouldn't count as separate pageviews.
  useEffect(() => {
    if (!pathname) return;
    const ph = getPostHog();
    if (!ph) return;
    ph.capture("$pageview", { pathname });
  }, [pathname]);

  return <>{children}</>;
}
