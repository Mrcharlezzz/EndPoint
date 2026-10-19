import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSession } from "./useSession";

function SessionProbe() {
  const { isAuthed, isResolved } = useSession();
  return (
    <div>
      <span data-testid="resolved">{String(isResolved)}</span>
      <span data-testid="authed">{String(isAuthed)}</span>
    </div>
  );
}

function createWrapper() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useSession → useConnections → apiFetch (fetch stubbed)", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("treats 200 connections as authenticated", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ results: [{ platform: "moodle", status: "connected", last_sync_at: null, expired_at: null }] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    render(<SessionProbe />, { wrapper: createWrapper() });

    await waitFor(() => expect(screen.getByTestId("resolved").textContent).toBe("true"));
    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8001/api/v1/connections/",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("treats 401 on connections as anonymous (resolved, not authed)", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not authenticated" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    );

    render(<SessionProbe />, { wrapper: createWrapper() });

    await waitFor(() => expect(screen.getByTestId("resolved").textContent).toBe("true"));
    expect(screen.getByTestId("authed").textContent).toBe("false");
  });
});
