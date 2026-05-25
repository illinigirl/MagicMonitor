import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

// Minimal Vitest config — picks up *.test.ts files anywhere under
// src/, aliases @/* to src/* to match the project's tsconfig
// import convention. No JSX/React component tests yet (lib-only),
// so the default `node` environment is correct — no jsdom needed.
export default defineConfig({
  test: {
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    environment: "node",
  },
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
});
