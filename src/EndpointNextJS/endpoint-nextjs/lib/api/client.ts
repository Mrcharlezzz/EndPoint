/**
 * Browser-side fetch wrapper for the presentation-api. All calls send the
 * session cookie via `credentials: 'include'`. Errors are normalized into
 * ApiError so call sites and React Query handlers can branch on shape.
 */

const PRESENTATION_API_URL =
  process.env.NEXT_PUBLIC_PRESENTATION_API_URL ?? "http://localhost:8001";

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
};

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, headers = {} } = options;

  const url = path.startsWith("http") ? path : `${PRESENTATION_API_URL}${path}`;
  const init: RequestInit = {
    method,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    signal,
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }

  const response = await fetch(url, init);

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload: unknown = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    const message =
      isJson && payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `Request failed: ${response.status}`;
    throw new ApiError(message, response.status, payload);
  }

  return payload as T;
}
