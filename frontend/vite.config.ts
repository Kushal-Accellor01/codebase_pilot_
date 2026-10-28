/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.PILOT_WEB_PORT ?? 5173),
    proxy: { "/api": { target: process.env.PILOT_API_URL ?? "http://localhost:8000", changeOrigin: true } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
    exclude: ["e2e/**", "node_modules/**", "dist/**"], // Playwright specs run with `npx playwright test`
    env: { VITE_API_MODE: "fixtures" },
  },
});
