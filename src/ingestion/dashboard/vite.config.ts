import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';
import path from 'node:path';

const DAEMON_URL = process.env.VITE_DAEMON_URL ?? 'http://localhost:8765';

function parsePort(value: string | undefined, fallback: number): number {
  if (!value) {
    return fallback;
  }

  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

const DEV_SERVER_PORT = parsePort(process.env.VITE_DEV_SERVER_PORT, 5173);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: DEV_SERVER_PORT,
    strictPort: true,
    // Vite 8 sometimes misdetects the HMR WebSocket URL when the HTTP
    // server also proxies WS upgrades. Pin it so the HMR client knows
    // exactly where to connect.
    hmr: { protocol: 'ws', host: 'localhost', port: DEV_SERVER_PORT },
    proxy: {
      // REST lives under /api; WebSocket channels live under /ws.
      // ws: true on the /ws entry forwards the upgrade handshake.
      '/api': { target: DAEMON_URL, changeOrigin: false },
      '/ws': { target: DAEMON_URL, changeOrigin: false, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
  },
});
