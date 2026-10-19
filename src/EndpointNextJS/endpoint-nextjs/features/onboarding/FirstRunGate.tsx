"use client";

import { useEffect, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";

import { useConnections } from "@/lib/queries/useConnections";
import { useCourses } from "@/lib/queries/useCourses";
import { useSession } from "@/lib/queries/useSession";

const ALLOWED_PATHS = new Set(["/onboarding", "/settings"]);

/**
 * Sends a brand-new authed user to /onboarding the first time they hit
 * the app. Runs after the initial dashboard render so the redirect only
 * fires once we know connection + course state — no server-side fetch
 * required.
 *
 * Skips the redirect when the user is already on /onboarding or
 * /settings, to avoid a redirect loop and to let "Re-run setup" work
 * from inside the app.
 */
export function FirstRunGate() {
  const router = useRouter();
  const pathname = usePathname();
  const { isAuthed, isResolved } = useSession();
  const { data: connections, isLoading: loadingConn } = useConnections();
  const { data: tracked, isLoading: loadingCourses } = useCourses({ enabled: isAuthed });
  const redirected = useRef(false);

  useEffect(() => {
    if (redirected.current) return;
    if (!isResolved || !isAuthed) return;
    if (loadingConn || loadingCourses) return;
    if (ALLOWED_PATHS.has(pathname)) return;

    const noConnections = (connections ?? []).length === 0;
    const noCourses = (tracked ?? []).length === 0;
    if (noConnections || noCourses) {
      redirected.current = true;
      router.replace("/onboarding?from=welcome");
    }
  }, [connections, tracked, loadingConn, loadingCourses, pathname, router, isAuthed, isResolved]);

  return null;
}
