import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";
import { defineConfig } from "vite";

function buildId(): string {
  try {
    const sha = execFileSync("git", ["rev-parse", "--short=12", "HEAD"], {
      encoding: "utf8",
      cwd: new URL(".", import.meta.url),
    }).trim();
    return `0.3.0+${sha || "unknown"}`;
  } catch {
    return "0.3.0+unknown";
  }
}

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_BUILD_ID__: JSON.stringify(buildId()),
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8099",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/@uiw") || id.includes("node_modules/@codemirror") || id.includes("@lezer")) {
            return "editor";
          }
          if (id.includes("node_modules/react-dom")) return "react-dom";
          if (id.includes("node_modules/react-router")) return "router";
          if (id.includes("node_modules/react/")) return "react";
          if (id.includes("node_modules/lucide-react")) return "icons";
          return undefined;
        },
      },
    },
  },
});
