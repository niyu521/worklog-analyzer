// Build script: bundles TypeScript entry points with esbuild and copies static
// assets (manifest, HTML, CSS) into dist/. Run `npm run build` or `npm run watch`.
import * as esbuild from "esbuild";
import { cp, mkdir, readdir, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.join(__dirname, "dist");
const srcdir = path.join(__dirname, "src");
const publicdir = path.join(__dirname, "public");

const entryPoints = {
  background: path.join(srcdir, "background.ts"),
  content: path.join(srcdir, "content.ts"),
  popup: path.join(srcdir, "popup.ts"),
  options: path.join(srcdir, "options.ts"),
};

const watch = process.argv.includes("--watch");

async function copyStatic() {
  // Copy everything in public/ (manifest.json, icons/, etc.) to dist/.
  if (existsSync(publicdir)) {
    await cp(publicdir, outdir, { recursive: true });
  }
  // Copy any HTML/CSS that live alongside the TS sources.
  for (const file of await readdir(srcdir)) {
    if (file.endsWith(".html") || file.endsWith(".css")) {
      await cp(path.join(srcdir, file), path.join(outdir, file));
    }
  }
}

async function main() {
  await rm(outdir, { recursive: true, force: true });
  await mkdir(outdir, { recursive: true });

  const options = {
    entryPoints,
    bundle: true,
    // IIFE keeps each entry point self-contained with no import/export in the
    // output, which is required for content scripts (classic scripts) and works
    // equally well for the service worker and popup/options pages.
    format: "iife",
    target: "chrome110",
    outdir,
    logLevel: "info",
  };

  if (watch) {
    const ctx = await esbuild.context(options);
    await ctx.watch();
    await copyStatic();
    console.log("watching…");
  } else {
    await esbuild.build(options);
    await copyStatic();
    console.log("build complete -> dist/");
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
