import path from "node:path";

import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import importPlugin from "eslint-plugin-import";

const root = path.dirname(new URL(import.meta.url).pathname);
const at = (rel) => path.resolve(root, rel);

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "coverage/**",
    "next-env.d.ts",
    ".corepack/**",
    ".pnpm-store/**",
    ".xdg-cache/**",
  ]),
  {
    plugins: { import: importPlugin },
    rules: {
      // Layer-direction enforcement.
      // domain/ depends on nothing. ui/ may import from domain only.
      // lib/api has no React or domain. lib/mappers can see lib/api + domain.
      // lib/queries can see lib/api + lib/mappers + domain. features/shell sit on top.
      "import/no-restricted-paths": [
        "error",
        {
          zones: [
            { target: at("domain"), from: at("app") },
            { target: at("domain"), from: at("shell") },
            { target: at("domain"), from: at("features") },
            { target: at("domain"), from: at("ui") },
            { target: at("domain"), from: at("lib") },

            { target: at("ui"), from: at("app") },
            { target: at("ui"), from: at("shell") },
            { target: at("ui"), from: at("features") },
            { target: at("ui"), from: at("lib") },

            { target: at("lib/api"), from: at("domain") },
            { target: at("lib/api"), from: at("ui") },
            { target: at("lib/api"), from: at("features") },
            { target: at("lib/api"), from: at("shell") },
            { target: at("lib/api"), from: at("app") },

            { target: at("lib/mappers"), from: at("ui") },
            { target: at("lib/mappers"), from: at("features") },
            { target: at("lib/mappers"), from: at("shell") },
            { target: at("lib/mappers"), from: at("app") },

            { target: at("lib/queries"), from: at("ui") },
            { target: at("lib/queries"), from: at("features") },
            { target: at("lib/queries"), from: at("shell") },
            { target: at("lib/queries"), from: at("app") },

            { target: at("shell"), from: at("app") },
          ],
        },
      ],
    },
  },
]);

export default eslintConfig;
