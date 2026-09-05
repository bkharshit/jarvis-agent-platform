import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, loadEnv } from "vite";

// The dev proxy is the single seam to the backend — no CORS middleware on
// the FastAPI app. A separately-deployed frontend (later) will need CORS.
//
// The target resolves from: real env vars > web/.env / .env.local
// (JARVIS_API_URL) > the :8000 default. web/.env is gitignored.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.JARVIS_API_URL ?? "http://127.0.0.1:8000";
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": "/src",
      },
    },
    server: {
      proxy: {
        "/v1": apiTarget,
        "/healthz": apiTarget,
      },
    },
  };
});