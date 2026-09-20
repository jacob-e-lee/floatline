// @ts-check
import { defineConfig } from "astro/config";
import react from "@astrojs/react";
import tailwindcss from "@tailwindcss/vite";

// https://astro.build/config
export default defineConfig({
  // React is used ONLY for the Recharts telemetry visualisation.
  integrations: [react()],
  vite: {
    plugins: [tailwindcss()],
    server: {
      // Local dev: forward API + SSE calls to the FastAPI backend on :8000.
      // In production Caddy does this routing instead (see /Caddyfile).
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          changeOrigin: true,
        },
      },
    },
  },
});