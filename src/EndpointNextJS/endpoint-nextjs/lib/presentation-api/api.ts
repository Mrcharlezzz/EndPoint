/**
 * Typed functions that perform Presentation API HTTP calls.
 *
 * Use these when you need explicit success/status handling, Server Actions, or Route Handlers.
 * For RSC pages that should **fall back to scaffold** when the API or env is missing, use
 * `server-data.ts` (`getAssignments`, `getAnnouncements`, …) instead.
 *
 * Env: `NEXT_PUBLIC_PRESENTATION_API_URL`, `PRESENTATION_API_BEARER_TOKEN` (see docs/presentation-api-local-testing.md).
 * Contract: `docs/artifacts/design-docs/presentation_api.md` (repo root).
 */
import type {
  AnnouncementDetail,
  AnnouncementsListResponse,
  CourseListItem,
  EventsListResponse,
  LoginRequest,
  LoginResponse,
  PresentationAssignment,
  PresentationEvent,
  PresentationUser,
  ResultsWrapper,
  UserCourseSelectionItem,
} from "./types";

export type PresentationApiResult<T> = {
  ok: boolean;
  status: number;
  data: T | null;
};

function apiBase(): string | null {
  const raw = process.env.NEXT_PUBLIC_PRESENTATION_API_URL?.trim();
  if (!raw) return null;
  return raw.replace(/\/$/, "");
}

function bearerToken(): string | null {
  const t = process.env.PRESENTATION_API_BEARER_TOKEN?.trim();
  return t || null;
}

function normalizePath(path: string): string {
  if (!path.startsWith("/")) return `/${path}`;
  return path;
}

async function presentationRequest<T>(
  path: string,
  init: {
    method?: "GET" | "POST" | "PUT" | "DELETE";
    body?: unknown;
  } = {},
): Promise<PresentationApiResult<T>> {
  const base = apiBase();
  if (!base) return { ok: false, status: 0, data: null };

  const p = normalizePath(path);
  const isLogin = p.includes("/api/v1/auth/login");
  const needsBearer = p.startsWith("/api/v1/") && !isLogin;

  let token: string | null = null;
  if (needsBearer) {
    token = bearerToken();
    if (!token) return { ok: false, status: 0, data: null };
  }

  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";

  const url = `${base}${p}`;
  const fetchInit: RequestInit = {
    method,
    headers,
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  };

  if (method === "GET") {
    fetchInit.next = { revalidate: 60 };
  } else {
    fetchInit.cache = "no-store";
  }

  try {
    const res = await fetch(url, fetchInit);
    if (res.status === 204) {
      return { ok: res.ok, status: res.status, data: null as T };
    }
    const text = await res.text();
    let data: T | null = null;
    if (text) {
      try {
        data = JSON.parse(text) as T;
      } catch {
        data = null;
      }
    }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

/** `GET /health/` — no auth. */
export async function presentationHealth(): Promise<PresentationApiResult<unknown>> {
  const base = apiBase();
  if (!base) return { ok: false, status: 0, data: null };
  try {
    const res = await fetch(`${base}/health/`, {
      headers: { Accept: "application/json" },
      next: { revalidate: 30 },
    });
    const text = await res.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text) as unknown;
      } catch {
        data = text;
      }
    }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

/** `POST /api/v1/auth/login/` — no bearer. */
export async function presentationLogin(
  body: LoginRequest,
): Promise<PresentationApiResult<LoginResponse>> {
  return presentationRequest<LoginResponse>("/api/v1/auth/login/", {
    method: "POST",
    body,
  });
}

/** `GET /api/v1/users/me/` */
export async function presentationGetMe(): Promise<PresentationApiResult<PresentationUser>> {
  return presentationRequest<PresentationUser>("/api/v1/users/me/");
}

/**
 * `DELETE /api/v1/users/me/` (design doc §6.2).
 * If your deployment uses `/api/v1/users/me/delete/` instead, switch the path here.
 */
export async function presentationDeleteMe(): Promise<PresentationApiResult<null>> {
  return presentationRequest<null>("/api/v1/users/me/delete/", { method: "DELETE" });
}

/** `GET /api/v1/users/me/courses/` */
export async function presentationGetMyCourses(): Promise<
  PresentationApiResult<ResultsWrapper<UserCourseSelectionItem>>
> {
  return presentationRequest<ResultsWrapper<UserCourseSelectionItem>>("/api/v1/users/me/courses/");
}

/** `PUT /api/v1/users/me/courses/` */
export async function presentationPutMyCourses(
  courseIds: string[],
): Promise<PresentationApiResult<ResultsWrapper<UserCourseSelectionItem>>> {
  return presentationRequest<ResultsWrapper<UserCourseSelectionItem>>("/api/v1/users/me/courses/", {
    method: "PUT",
    body: { course_ids: courseIds },
  });
}

/** `GET /api/v1/courses/` */
export async function presentationGetCourses(): Promise<
  PresentationApiResult<ResultsWrapper<CourseListItem>>
> {
  return presentationRequest<ResultsWrapper<CourseListItem>>("/api/v1/courses/");
}

/** `GET /api/v1/assignments/` */
export async function presentationGetAssignments(params?: {
  course_id?: string;
  platform?: string;
  status?: string;
}): Promise<PresentationApiResult<ResultsWrapper<PresentationAssignment>>> {
  const q = new URLSearchParams();
  if (params?.course_id) q.set("course_id", params.course_id);
  if (params?.platform) q.set("platform", params.platform);
  if (params?.status) q.set("status", params.status);
  const qs = q.toString();
  const path = qs ? `/api/v1/assignments/?${qs}` : "/api/v1/assignments/";
  return presentationRequest<ResultsWrapper<PresentationAssignment>>(path);
}

/** `GET /api/v1/assignments/{id}/` */
export async function presentationGetAssignment(
  id: string,
): Promise<PresentationApiResult<PresentationAssignment>> {
  return presentationRequest<PresentationAssignment>(
    `/api/v1/assignments/${encodeURIComponent(id)}/`,
  );
}

/** `GET /api/v1/announcements/` */
export async function presentationGetAnnouncements(params?: {
  course_id?: string;
  platform?: string;
}): Promise<PresentationApiResult<AnnouncementsListResponse>> {
  const q = new URLSearchParams();
  if (params?.course_id) q.set("course_id", params.course_id);
  if (params?.platform) q.set("platform", params.platform);
  const qs = q.toString();
  const path = qs ? `/api/v1/announcements/?${qs}` : "/api/v1/announcements/";
  return presentationRequest<AnnouncementsListResponse>(path);
}

/** `GET /api/v1/announcements/{id}/` */
export async function presentationGetAnnouncement(
  id: string,
): Promise<PresentationApiResult<AnnouncementDetail>> {
  return presentationRequest<AnnouncementDetail>(
    `/api/v1/announcements/${encodeURIComponent(id)}/`,
  );
}

/** `GET /api/v1/events/` */
export async function presentationGetEvents(params?: {
  course_id?: string;
}): Promise<PresentationApiResult<EventsListResponse>> {
  const q = new URLSearchParams();
  if (params?.course_id) q.set("course_id", params.course_id);
  const qs = q.toString();
  const path = qs ? `/api/v1/events/?${qs}` : "/api/v1/events/";
  return presentationRequest<EventsListResponse>(path);
}

/** `GET /api/v1/events/{id}/` */
export async function presentationGetEvent(
  id: string,
): Promise<PresentationApiResult<PresentationEvent>> {
  return presentationRequest<PresentationEvent>(`/api/v1/events/${encodeURIComponent(id)}/`);
}
