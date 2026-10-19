import { apiFetch } from "./client";
import type { AuthApiUser } from "./types";

export function fetchMe(): Promise<AuthApiUser> {
  return apiFetch<AuthApiUser>("/api/v1/users/me/");
}

export function deleteMe(): Promise<void> {
  return apiFetch<void>("/api/v1/users/me/delete/", { method: "DELETE" });
}
