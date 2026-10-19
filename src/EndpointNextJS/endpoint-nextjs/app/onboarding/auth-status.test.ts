import assert from "node:assert/strict";
import test from "node:test";

import { hasValidSessionToken } from "./auth-status";

test("returns false when there is no session token", async () => {
  const isValid = await hasValidSessionToken(undefined);

  assert.equal(isValid, false);
});

test("returns false when the presentation API base URL is missing", async () => {
  const isValid = await hasValidSessionToken("token", {
    baseUrl: "   ",
  });

  assert.equal(isValid, false);
});

test("returns true when the presentation API accepts the session token", async () => {
  let receivedUrl = "";
  let receivedInit: RequestInit | undefined;

  const isValid = await hasValidSessionToken("token-123", {
    baseUrl: "http://localhost:8001/",
    fetchImpl: async (url, init) => {
      receivedUrl = String(url);
      receivedInit = init;
      return new Response(JSON.stringify({ id: "user-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });

  assert.equal(isValid, true);
  assert.equal(receivedUrl, "http://localhost:8001/api/v1/users/me/");
  assert.equal(receivedInit?.cache, "no-store");
  assert.deepEqual(receivedInit?.headers, {
    Accept: "application/json",
    Authorization: "Bearer token-123",
  });
});

test("returns false when the presentation API rejects the session token", async () => {
  const isValid = await hasValidSessionToken("stale-token", {
    fetchImpl: async () =>
      new Response(JSON.stringify({ error: { code: "authentication_required" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
  });

  assert.equal(isValid, false);
});

test("returns false when the session lookup throws", async () => {
  const isValid = await hasValidSessionToken("token-123", {
    fetchImpl: async () => {
      throw new Error("network down");
    },
  });

  assert.equal(isValid, false);
});
