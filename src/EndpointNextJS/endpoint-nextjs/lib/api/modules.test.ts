import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchAnnouncements } from "./announcements";
import { fetchAssignment, fetchAssignments } from "./assignments";
import { getMe, logout, moodleLogin, teamsFinalize } from "./auth";
import { ApiError } from "./client";
import {
  attachMoodleConnection,
  attachTeamsConnection,
  disconnectPlatform,
  fetchConnections,
  reconnectPlatform,
} from "./connections";
import { fetchCatalog, fetchTrackedCourses, setTrackedCourses } from "./courses";
import { fetchEvents } from "./events";
import { fetchPreferences, setPreferences } from "./preferences";
import { clearReadState, fetchReadState, markRead } from "./readState";
import { deleteMe, fetchMe } from "./users";

function jsonOk(data: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function lastFetch(): { url: string; init: RequestInit } {
  expect(fetch).toHaveBeenCalled();
  const calls = vi.mocked(fetch).mock.calls;
  const [url, init] = calls[calls.length - 1]!;
  return { url: String(url), init: init ?? {} };
}

describe("lib/api — courses", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchCatalog GET /api/v1/courses/", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchCatalog();
    const { url, init } = lastFetch();
    expect(url).toBe("http://localhost:8001/api/v1/courses/");
    expect(init.method).toBe("GET");
  });

  it("fetchTrackedCourses GET /api/v1/users/me/courses/", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchTrackedCourses();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/users/me/courses/");
  });

  it("setTrackedCourses PUT body course_ids", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await setTrackedCourses(["a", "b"]);
    const { url, init } = lastFetch();
    expect(url).toBe("http://localhost:8001/api/v1/users/me/courses/");
    expect(init.method).toBe("PUT");
    expect(init.body).toBe(JSON.stringify({ course_ids: ["a", "b"] }));
  });
});

describe("lib/api — connections", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchConnections", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchConnections();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/connections/");
  });

  it("disconnectPlatform DELETE", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await disconnectPlatform("moodle");
    const { url, init } = lastFetch();
    expect(url).toBe("http://localhost:8001/api/v1/connections/moodle/");
    expect(init.method).toBe("DELETE");
  });

  it("reconnectPlatform POST", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await reconnectPlatform("teams");
    const { url, init } = lastFetch();
    expect(url).toBe("http://localhost:8001/api/v1/connections/teams/reconnect/");
    expect(init.method).toBe("POST");
  });

  it("attachTeamsConnection POST session_id", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await attachTeamsConnection("sess-1");
    const { init } = lastFetch();
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ session_id: "sess-1" }));
  });

  it("attachMoodleConnection POST credentials", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await attachMoodleConnection({ username: "u", password: "p" });
    expect(lastFetch().init.body).toBe(JSON.stringify({ username: "u", password: "p" }));
  });
});

describe("lib/api — users", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchMe", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ id: "1", email: "a@b.c", display_name: "A", created_at: "t" }));
    await fetchMe();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/users/me/");
  });

  it("deleteMe DELETE", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await deleteMe();
    const { url, init } = lastFetch();
    expect(url).toBe("http://localhost:8001/api/v1/users/me/delete/");
    expect(init.method).toBe("DELETE");
  });
});

describe("lib/api — assignments", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchAssignments without query", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchAssignments();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/assignments/");
  });

  it("fetchAssignments with course_id and platform", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchAssignments({ courseId: "c1", platform: "moodle" });
    expect(lastFetch().url).toBe(
      "http://localhost:8001/api/v1/assignments/?course_id=c1&platform=moodle",
    );
  });

  it("fetchAssignment by id", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({
        id: "x",
        title: "t",
        body: null,
        author: "a",
        course: { id: "c", name: "n", code: "co" },
        deadline: null,
        submission_format: "f",
        platform: "moodle",
        source_url: null,
        links: [],
        attachments: [],
        created_at: "t",
      }),
    );
    await fetchAssignment("x");
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/assignments/x/");
  });
});

describe("lib/api — announcements", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchAnnouncements with query string", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchAnnouncements({ courseId: "c2", platform: "teams" });
    expect(lastFetch().url).toBe(
      "http://localhost:8001/api/v1/announcements/?course_id=c2&platform=teams",
    );
  });
});

describe("lib/api — readState", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchReadState", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({}));
    await fetchReadState();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/users/me/read-state/");
  });

  it("markRead POST item_ids", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await markRead(["a", "b"]);
    expect(lastFetch().init.method).toBe("POST");
    expect(lastFetch().init.body).toBe(JSON.stringify({ item_ids: ["a", "b"] }));
  });

  it("clearReadState DELETE", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await clearReadState();
    expect(lastFetch().init.method).toBe("DELETE");
  });
});

describe("lib/api — preferences", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchPreferences", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ theme: "dark" }));
    await fetchPreferences();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/users/me/preferences/");
  });

  it("setPreferences PUT", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ theme: "light" }));
    await setPreferences({ theme: "light" });
    const { init } = lastFetch();
    expect(init.method).toBe("PUT");
    expect(init.body).toBe(JSON.stringify({ theme: "light" }));
  });
});

describe("lib/api — events", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("fetchEvents with course_id", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ results: [] }));
    await fetchEvents({ courseId: "c9" });
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/events/?course_id=c9");
  });
});

describe("lib/api — auth", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("teamsFinalize POST", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({ user: { id: "1", email: "e", display_name: "D", created_at: "t" }, expires_at: "t" }),
    );
    await teamsFinalize("sid");
    expect(lastFetch().init.body).toBe(JSON.stringify({ session_id: "sid" }));
  });

  it("moodleLogin POST", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({ user: { id: "1", email: "e", display_name: "D", created_at: "t" }, expires_at: "t" }),
    );
    await moodleLogin({ username: "u", password: "p" });
    expect(lastFetch().init.body).toBe(JSON.stringify({ username: "u", password: "p" }));
  });

  it("logout POST", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));
    await logout();
    expect(lastFetch().url).toBe("http://localhost:8001/api/v1/auth/logout/");
    expect(lastFetch().init.method).toBe("POST");
  });

  it("getMe returns null on 401", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "nope" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(getMe()).resolves.toBeNull();
  });

  it("getMe rethrows non-401 ApiError", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "bad" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(getMe()).rejects.toBeInstanceOf(ApiError);
  });
});
