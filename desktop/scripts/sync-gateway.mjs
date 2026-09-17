#!/usr/bin/env node
/**
 * Copy gateway Python sources into src-tauri/resources/gateway for bundling.
 * Never copies .env, token caches, or media.
 */
import { cpSync, mkdirSync, rmSync, existsSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const desktop = resolve(__dirname, "..");
const repo = resolve(desktop, "..");
const dest = join(desktop, "src-tauri", "resources", "gateway");

const files = [
  "grokbot2api.py",
  "sand_inference.py",
  "api_common.py",
  "messages_api.py",
  "responses_api.py",
  "model_catalogue.py",
  "image_gen.py",
];

mkdirSync(dest, { recursive: true });
for (const name of files) {
  const src = join(repo, name);
  if (!existsSync(src)) {
    console.error(`[sync-gateway] missing ${src}`);
    process.exit(1);
  }
  cpSync(src, join(dest, name));
}

// empty media placeholder so relative paths exist
mkdirSync(join(dest, "media"), { recursive: true });
writeFileSync(join(dest, "media", ".gitkeep"), "");
writeFileSync(
  join(dest, "README.txt"),
  "Bundled grokbot2api gateway sources. Launched by the Tauri host via system Python.\n",
);

console.log(`[sync-gateway] copied ${files.length} modules → ${dest}`);
