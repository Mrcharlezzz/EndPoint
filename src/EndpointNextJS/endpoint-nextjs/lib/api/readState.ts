import { apiFetch } from "./client";

export type ReadStateMap = Record<string, string>;

export function fetchReadState(): Promise<ReadStateMap> {
  return apiFetch<ReadStateMap>("/api/v1/users/me/read-state/");
}

export function markRead(itemIds: string[]): Promise<void> {
  return apiFetch<void>("/api/v1/users/me/read-state/", {
    method: "POST",
    body: { item_ids: itemIds },
  });
}

export function clearReadState(): Promise<void> {
  return apiFetch<void>("/api/v1/users/me/read-state/", { method: "DELETE" });
}
