import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(root, "./"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.{test,spec}.{ts,tsx}"],
    exclude: [
      "node_modules",
      ".next",
      "out",
      // Onboarding logic tests use `node:test`; run with `node --test` instead.
      "**/app/onboarding/flow-config.test.ts",
      "**/app/onboarding/auth-status.test.ts",
    ],
    passWithNoTests: false,
    coverage: {
      provider: "v8",
      reportsDirectory: "./coverage",
      reporter: ["text", "html", "json-summary"],
      exclude: [
        "node_modules/**",
        ".next/**",
        "out/**",
        "coverage/**",
        "**/*.config.*",
        "**/*.test.*",
        "**/*.spec.*",
        "**/*.integration.test.*",
        "next-env.d.ts",
      ],
    },
  },
});
