import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: Object.fromEntries(
      ['/v1', '/health', '/docs', '/openapi.json'].map((path) => [path, 'http://127.0.0.1:8000']),
    ),
  },
});
