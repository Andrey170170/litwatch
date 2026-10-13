/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// The build lands inside the Python package, where `litwatch serve` serves it at `/`.
// `LITWATCH_API` points the dev proxy at another port (`LITWATCH_API=http://127.0.0.1:8767 pnpm dev`).
const api = process.env.LITWATCH_API ?? "http://127.0.0.1:8766";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  build: { outDir: path.resolve(import.meta.dirname, "../src/litwatch/api/web"), emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": api } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], include: ["src/**/*.test.{ts,tsx}"] },
});
