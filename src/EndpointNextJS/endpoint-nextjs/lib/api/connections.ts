import { apiFetch } from "./client";
import type { ApiConnection, ApiList } from "./types";

export function fetchConnections(): Promise<ApiList<ApiConnection>> {
  return apiFetch<ApiList<ApiConnection>>("/api/v1/connections/");
}

export function disconnectPlatform(platform: "moodle" | "teams"): Promise<void> {
  return apiFetch<void>(`/api/v1/connections/${platform}/`, { method: "DELETE" });
}

export function reconnectPlatform(platform: "moodle" | "teams"): Promise<void> {
  return apiFetch<void>(`/api/v1/connections/${platform}/reconnect/`, {
    method: "POST",
  });
}

export function attachTeamsConnection(sessionId: string): Promise<void> {
  return apiFetch<void>("/api/v1/connections/teams/finalize/", {
    method: "POST",
    body: { session_id: sessionId },
  });
}

export function attachMoodleConnection(input: {
  username: string;
  password: string;
}): Promise<void> {
  return apiFetch<void>("/api/v1/connections/moodle/", {
    method: "POST",
    body: input,
  });
}
