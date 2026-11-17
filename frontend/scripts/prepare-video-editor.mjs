// Scope upstream styles and remove optional remote fonts for the local editor.
import { readFile, writeFile } from "node:fs/promises";
const css = await readFile(
  new URL(
    "../node_modules/@twick/video-editor/dist/video-editor.css",
    import.meta.url,
  ),
  "utf8",
);
await writeFile(
  new URL("../src/video-twick.generated.css", import.meta.url),
  "/* Generated from @twick/video-editor, whose license remains in node_modules. */\n@scope (.video-workspace) {\n" +
    css.replace(/@import\s+url\([^;]+\);/g, "").replace(/:root/g, ":scope") +
    "\n}\n",
);
