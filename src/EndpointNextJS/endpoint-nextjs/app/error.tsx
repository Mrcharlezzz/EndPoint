"use client";

import { useEffect } from "react";

import { log } from "@/lib/logger";

export default function RootError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    log.error("app.error", error, { digest: error.digest });
  }, [error]);

  return (
    <main style={{ padding: "2rem", maxWidth: "640px", margin: "0 auto" }}>
      <h1 style={{ fontSize: "1.25rem", fontWeight: 600 }}>Something went wrong</h1>
      <p style={{ marginTop: "0.5rem", color: "var(--text-muted)" }}>
        Try again, or reload the page if the problem persists.
      </p>
      <button
        type="button"
        onClick={reset}
        style={{
          marginTop: "1rem",
          padding: "0.5rem 1rem",
          background: "var(--accent)",
          color: "var(--accent-fg)",
          borderRadius: "var(--radius)",
        }}
      >
        Try again
      </button>
    </main>
  );
}
