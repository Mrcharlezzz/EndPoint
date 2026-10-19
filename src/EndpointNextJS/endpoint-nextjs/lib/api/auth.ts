import { apiFetch } from "./client";
import type { AuthApiUser, AuthFinalizeResponse } from "./types";

export async function teamsFinalize(sessionId: string): Promise<AuthFinalizeResponse> {
  return apiFetch<AuthFinalizeResponse>("/api/v1/auth/teams/finalize/", {
    method: "POST",
    body: { session_id: sessionId },
  });
}

export async function moodleLogin(input: {
  username: string;
  password: string;
}): Promise<AuthFinalizeResponse> {
  return apiFetch<AuthFinalizeResponse>("/api/v1/auth/moodle/", {
    method: "POST",
    body: input,
  });
}

export async function logout(): Promise<void> {
  await apiFetch<void>("/api/v1/auth/logout/", { method: "POST" });
}

export async function getMe(): Promise<AuthApiUser | null> {
  try {
    return await apiFetch<AuthApiUser>("/api/v1/users/me/");
  } catch (err) {
    if (err && typeof err === "object" && "status" in err && (err as { status: number }).status === 401) {
      return null;
    }
    throw err;
  }
}
