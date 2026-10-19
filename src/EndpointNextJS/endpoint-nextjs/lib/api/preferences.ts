import { apiFetch } from "./client";
import type { ApiPreferences } from "./types";

export function fetchPreferences(): Promise<ApiPreferences> {
  return apiFetch<ApiPreferences>("/api/v1/users/me/preferences/");
}

export function setPreferences(prefs: ApiPreferences): Promise<ApiPreferences> {
  return apiFetch<ApiPreferences>("/api/v1/users/me/preferences/", {
    method: "PUT",
    body: prefs,
  });
}
