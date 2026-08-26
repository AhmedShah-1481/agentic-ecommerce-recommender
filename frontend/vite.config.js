import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // strictPort, not the default "find a free one": the API's CORS_ORIGINS
    // whitelists this exact origin, and allow_credentials=True means a browser
    // will silently drop the session cookie if Vite quietly moves to :5174.
    // Failing to boot is a much better signal than being mysteriously signed out.
    port: 5173,
    strictPort: true,
  },
});
