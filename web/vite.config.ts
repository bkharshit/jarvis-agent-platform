import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// The dev proxy is the single seam to the backend — no CORS middleware on
// the FastAPI app. A separately-deployed frontend (later) will need CORS.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": "/src",
    },
  },
  server: {
    proxy: {
      // JARVIS_API_URL lets a second backend instance run alongside the
      // default :8000 (e.g. the e2e smoke on :8001).
      "/v1": process.env.JARVIS_API_URL ?? "http://127.0.0.1:8000",
      "/healthz": process.env.JARVIS_API_URL ?? "http://127.0.0.1:8000",
    },
  },
});