import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],build:{outDir:'../webui',emptyOutDir:true,sourcemap:false},server:{port:18861,proxy:{'/api':'http://127.0.0.1:18860'}}})
