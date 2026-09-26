import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  // The backend is the source of truth for every number on screen; proxying
  // keeps the browser same-origin so CORS never becomes a surprise.
  // BACKEND_URL (shell env or .env*) points the proxy at a backend on another port.
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const backend = env.BACKEND_URL || 'http://localhost:8000';
  return {
    plugins: [react()],
    server: {
      port: Number(env.PORT) || 5173,
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/ws': { target: backend.replace(/^http/, 'ws'), ws: true },
      },
    },
    build: {
      rollupOptions: {
        output: {
          // Map and chart libraries change rarely; keep them out of the app chunk.
          manualChunks: {
            deck: ['@deck.gl/core', '@deck.gl/layers', '@deck.gl/react'],
            charts: ['recharts'],
          },
        },
      },
      chunkSizeWarningLimit: 2500,
    },
  };
});
