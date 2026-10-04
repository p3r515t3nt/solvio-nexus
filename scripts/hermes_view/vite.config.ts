import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import tailwind from '@tailwindcss/vite';
import path from 'node:path';
export default defineConfig({
  base:'/dashboard/hermes/', publicDir:false, plugins:[react(),tailwind()],
  resolve:{alias:{'@':path.resolve(import.meta.dirname,'src'),
    '@hermes/shared':path.resolve(import.meta.dirname,'../apps/shared/src/index.ts')}},
  build:{outDir:'../solvio-view-dist',emptyOutDir:true,
    rolldownOptions:{input:path.resolve(import.meta.dirname,'solvio-view.html')}}
});
