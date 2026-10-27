import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// This proxy is local development plumbing, not a deployment routing design.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
});
