import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "path";

export default defineConfig({
  root: path.resolve(__dirname),
  base: "/static/collaborative-editor/",
  plugins: [vue()],
  build: {
    outDir: path.resolve(__dirname, "../../static/collaborative-editor"),
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: path.resolve(__dirname, "src/main.js"),
      output: {
        entryFileNames: "editor.js",
        assetFileNames: "editor.[ext]",
      },
    },
  },
});
