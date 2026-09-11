import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/api": "http://127.0.0.1:8000",
      // Dev-only proxy. The target stays http:// and `ws: true` performs the
      // upgrade -- a ws:// target makes http-proxy skip the HTTP handshake and
      // every WebSocket connection fails, which shows up in the UI as a
      // permanent "重连中…" with no captions arriving live.
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true,
        changeOrigin: true
      }
    }
  }
});

