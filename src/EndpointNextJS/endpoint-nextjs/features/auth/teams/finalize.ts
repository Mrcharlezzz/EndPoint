import { teamsFinalize } from "@/lib/api/auth";
import { log } from "@/lib/logger";
import { toAuthUser } from "@/lib/mappers/auth";

import { useAuthStore } from "../store";

/**
 * Called once the headless session reaches CAPTURED. Hands the session_id
 * to the presentation-api which pulls the captured payload server-to-server
 * and persists the PlatformCredential. Drives the AuthStore through the
 * authenticating → authed transition.
 */
export async function finalizeTeamsSession(sessionId: string): Promise<void> {
  const auth = useAuthStore.getState();
  auth.setStatus("authenticating");
  try {
    const { user } = await teamsFinalize(sessionId);
    auth.setUser(toAuthUser(user));
  } catch (err) {
    log.error("auth.teams.finalize.failed", err);
    const message = err instanceof Error ? err.message : "Sign-in failed.";
    auth.setStatus("error", message);
    throw err;
  }
}
