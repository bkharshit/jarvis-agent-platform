import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": "/src",
    },
  },
  test: {
    environment: "jsdom",
    environmentOptions: {
      // Base URL so same-origin relative request paths (the Vite proxy seam)
      // resolve — jsdom's default `about:blank` cannot.
      jsdom: { url: "http://localhost" },
    },
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});