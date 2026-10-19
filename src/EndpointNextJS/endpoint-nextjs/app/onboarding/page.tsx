import { cookies } from "next/headers";

import { hasValidSessionToken } from "./auth-status";
import { OnboardingFlow } from "./OnboardingFlow";

const SESSION_COOKIE = "endpoint_session";

type SearchParams = Promise<{ from?: string }>;

/**
 * Single entry point for the wizard. Handles both first-time visitors
 * (no session yet — flow starts at welcome and walks through moodle +
 * teams to create the account) and returning users who need to finish
 * setup (skips ahead to the first incomplete step). The `?from=welcome`
 * query param forces the full walk for "Re-run setup" from the Data tab.
 */
export default async function OnboardingPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const store = await cookies();
  const sessionToken = store.get(SESSION_COOKIE)?.value;
  const isAuthed = await hasValidSessionToken(sessionToken);
  const params = await searchParams;
  const forceFullFlow = params.from === "welcome";
  return <OnboardingFlow isAuthed={isAuthed} forceFullFlow={forceFullFlow} />;
}
