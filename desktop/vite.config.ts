import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

const VERSION: string = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")).version;

// Политика безопасности — только в сборке: в разработке React Refresh вставляет встроенный скрипт.
const CSP =
  "default-src 'self'; img-src 'self' data: blob: https: http:; style-src 'self' 'unsafe-inline'; " +
  "font-src 'self' data:; connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*; media-src 'self' data: blob:; " +
  "script-src 'self'";

function csp(): Plugin {
  return {
    name: "jarvis-csp",
    apply: "build",
    transformIndexHtml(html) {
      return html.replace("<head>", `<head>\n    <meta http-equiv="Content-Security-Policy" content="${CSP}" />`);
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), csp()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  define: { __APP_VERSION__: JSON.stringify(VERSION) }, // версия для браузерного режима (в приложении — от Electron)
  server: { port: 5173, strictPort: true, host: "127.0.0.1" },
  build: { outDir: "dist", emptyOutDir: true, target: "chrome130", chunkSizeWarningLimit: 2000 },
});
