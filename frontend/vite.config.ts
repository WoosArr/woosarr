import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:31337" } },
  // Unit tests only; Playwright specs live in e2e/.
  test: { include: ["src/**/*.test.ts?(x)"] },
});
