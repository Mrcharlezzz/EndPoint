import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch } from "./client";

describe("apiFetch", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("joins relative paths to the presentation API base and sends credentials", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await apiFetch<{ ok: boolean }>("/api/v1/ping/");

    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8001/api/v1/ping/",
      expect.objectContaining({
        method: "GET",
        credentials: "include",
      }),
    );
  });

  it("returns parsed JSON on 200", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ results: [{ id: "1" }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const data = await apiFetch<{ results: { id: string }[] }>("/api/v1/items/");
    expect(data.results[0]?.id).toBe("1");
  });

  it("returns undefined on 204", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiFetch<void>("/api/v1/nothing/")).resolves.toBeUndefined();
  });

  it("throws ApiError using JSON detail when present", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid token" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(apiFetch("/api/v1/protected/")).rejects.toEqual(
      expect.objectContaining({
        name: "ApiError",
        status: 401,
        message: "Invalid token",
      }),
    );
  });

  it("throws ApiError with a generic message when body is not JSON", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("Gateway timeout", {
        status: 504,
        headers: { "content-type": "text/plain" },
      }),
    );

    try {
      await apiFetch("/api/v1/slow/");
      expect.fail("expected throw");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(504);
      expect((e as ApiError).message).toMatch(/504/);
    }
  });
});
