#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════
//  Moonwalk — Upload DMG + Chrome Extension to GCS
//
//  Usage:
//    npm run dist:upload
//
//  Environment variables:
//    GCP_PROJECT  — GCP project ID (default: from deploy script)
//    GCS_BUCKET   — Bucket name (default: ${GCP_PROJECT}-moonwalk-releases)
//    GCS_PREFIX   — Object prefix (default: releases/)
// ═══════════════════════════════════════════════════════════════

import { execSync } from "node:child_process";
import { readdirSync, existsSync, statSync } from "node:fs";
import { join, basename } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const DIST = join(ROOT, "dist");
const EXT_ZIP = join(DIST, "moonwalk-browser-bridge.zip");

const GCP_PROJECT = process.env.GCP_PROJECT || "gen-lang-client-0333982983";
const GCS_BUCKET = process.env.GCS_BUCKET || "getmoonwalk.top";
const GCS_PREFIX = process.env.GCS_PREFIX || "releases/";
const PUBLIC_BASE = process.env.PUBLIC_BASE || `https://getmoonwalk.top`;
const VERSION = JSON.parse(
  execSync("cat package.json", { cwd: ROOT, encoding: "utf8" })
).version;

function run(cmd) {
  console.log(`  → ${cmd}`);
  execSync(cmd, { stdio: "inherit" });
}

function ensureBucket() {
  try {
    execSync(`gsutil ls -b gs://${GCS_BUCKET} 2>/dev/null`, { stdio: "pipe" });
    console.log(`✅ Bucket gs://${GCS_BUCKET} exists`);
  } catch {
    console.log(`📦 Creating bucket gs://${GCS_BUCKET}...`);
    run(`gsutil mb -p ${GCP_PROJECT} -l us-central1 -b on gs://${GCS_BUCKET}`);
    // Make objects publicly readable
    run(`gsutil iam ch allUsers:objectViewer gs://${GCS_BUCKET}`);
    console.log(`✅ Bucket created and made public`);
  }
}

function uploadFile(localPath, gcsKey) {
  const dest = `gs://${GCS_BUCKET}/${GCS_PREFIX}${gcsKey}`;
  console.log(`📤 Uploading ${basename(localPath)} → ${dest}`);
  run(`gsutil -h "Cache-Control:public,max-age=3600" cp "${localPath}" "${dest}"`);
  const url = `${PUBLIC_BASE}/${GCS_PREFIX}${gcsKey}`;
  return url;
}

// ── Main ──

console.log(`\n🌙 Moonwalk Release Uploader v${VERSION}\n`);

// 1. Ensure gcloud is available
try {
  execSync("which gsutil", { stdio: "pipe" });
} catch {
  console.error("❌ gsutil not found. Install: brew install --cask google-cloud-sdk");
  process.exit(1);
}

// 2. Ensure bucket
ensureBucket();

// 3. Find DMG(s) in dist/
if (!existsSync(DIST)) {
  console.error(`❌ dist/ folder not found. Run 'npm run build:signed' first.`);
  process.exit(1);
}

const dmgs = readdirSync(DIST).filter(
  (f) => f.endsWith(".dmg") && statSync(join(DIST, f)).isFile()
);

const urls = [];

// Upload DMGs
for (const dmg of dmgs) {
  const url = uploadFile(join(DIST, dmg), `v${VERSION}/${dmg}`);
  urls.push({ name: dmg, url });
}

// Upload latest alias
if (dmgs.length > 0) {
  const primary = dmgs.find((d) => d.includes("universal")) || dmgs[0];
  const latestUrl = uploadFile(
    join(DIST, primary),
    "latest/Moonwalk-latest.dmg"
  );
  urls.push({ name: "Moonwalk-latest.dmg (alias)", url: latestUrl });
}

// 4. Upload Chrome extension zip
if (existsSync(EXT_ZIP)) {
  const extUrl = uploadFile(EXT_ZIP, `v${VERSION}/moonwalk-browser-bridge.zip`);
  urls.push({ name: "moonwalk-browser-bridge.zip", url: extUrl });
  const latestExtUrl = uploadFile(EXT_ZIP, "latest/moonwalk-browser-bridge.zip");
  urls.push({ name: "Extension (latest alias)", url: latestExtUrl });
} else {
  console.log(
    `⚠️  No extension zip found at ${EXT_ZIP}. Run 'npm run dist:extension' first.`
  );
}

// 5. Generate download page from template
import { writeFileSync, readFileSync } from "node:fs";

const templatePath = new URL("landing-page.html", import.meta.url).pathname;
if (!existsSync(templatePath)) {
  console.error("❌ landing-page.html template not found at", templatePath);
  process.exit(1);
}

// Find the canonical DMG and extension URLs already uploaded
const dmgEntry = urls.find(u => u.name.includes("universal") || (u.name.endsWith(".dmg") && !u.name.includes("alias")));
const extEntry = urls.find(u => u.name === "moonwalk-browser-bridge.zip");

const dmgUrl = dmgEntry?.url ?? `https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PREFIX}latest/Moonwalk-latest.dmg`;
const extUrl = extEntry?.url ?? `https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PREFIX}latest/moonwalk-browser-bridge.zip`;

const landingHtml = readFileSync(templatePath, "utf8")
  .replaceAll("__VERSION__", VERSION)
  .replaceAll("__DMG_URL__", dmgUrl)
  .replaceAll("__EXT_URL__", extUrl);

const downloadPagePath = join(DIST, "index.html");
writeFileSync(downloadPagePath, landingHtml);

// Upload with no-cache so customers always get the latest page
run(`gsutil -h "Cache-Control:no-cache,no-store" -h "Content-Type:text/html" cp "${downloadPagePath}" "gs://${GCS_BUCKET}/${GCS_PREFIX}index.html"`);
const pageUrl = `${PUBLIC_BASE}/${GCS_PREFIX}index.html`;
urls.push({ name: "Download Page", url: pageUrl });

// 5b. Upload blog pages
const blogDir = new URL(".", import.meta.url).pathname;
const blogFiles = [
  { src: join(blogDir, "blog-how-we-made-moonwalk.html"), dest: "blog/how-we-made-moonwalk/index.html" },
];
for (const { src, dest } of blogFiles) {
  if (existsSync(src)) {
    console.log(`📝 Uploading blog → gs://${GCS_BUCKET}/${dest}`);
    run(`gsutil -h "Cache-Control:no-cache,no-store" -h "Content-Type:text/html" cp "${src}" "gs://${GCS_BUCKET}/${dest}"`);
    urls.push({ name: `Blog: ${dest}`, url: `${PUBLIC_BASE}/${dest}` });
  }
}

// 6. Summary
console.log(`\n${"═".repeat(60)}`);
console.log(`  🌙 Moonwalk v${VERSION} — Published to GCS`);
console.log(`${"═".repeat(60)}\n`);
console.log(`  📥 Download page:`);
  console.log(`     ${PUBLIC_BASE}/${GCS_PREFIX}index.html\n`);
for (const { name, url } of urls) {
  console.log(`  📦 ${name}`);
  console.log(`     ${url}\n`);
}
console.log(`${"═".repeat(60)}\n`);
