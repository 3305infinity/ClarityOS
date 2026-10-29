#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════
//  Moonwalk — Package Chrome Extension for Distribution
//
//  Creates a customer-ready .zip of the Chrome extension with
//  an install guide (README) inside it.
//
//  Usage:
//    npm run dist:extension
//
//  Output:
//    dist/moonwalk-browser-bridge.zip
// ═══════════════════════════════════════════════════════════════

import { execSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync, copyFileSync, readdirSync } from "node:fs";
import { join, basename } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const EXT_SRC = join(ROOT, "chrome_extension");
const DIST = join(ROOT, "dist");
const STAGING = join(DIST, "moonwalk-browser-bridge");

// ── Clean & create staging directory ──
if (existsSync(STAGING)) {
  execSync(`rm -rf "${STAGING}"`);
}
mkdirSync(STAGING, { recursive: true });
mkdirSync(DIST, { recursive: true });

// ── Copy extension files ──
const SKIP = new Set([".bak", ".DS_Store"]);

function copyDir(src, dest) {
  mkdirSync(dest, { recursive: true });
  for (const entry of readdirSync(src, { withFileTypes: true })) {
    if (SKIP.has(entry.name) || entry.name.endsWith(".bak")) continue;
    const srcPath = join(src, entry.name);
    const destPath = join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      copyFileSync(srcPath, destPath);
    }
  }
}

console.log("📦 Copying extension files...");
copyDir(EXT_SRC, STAGING);

// ── Write the customer-friendly README inside the zip ──
const readmeContent = `# 🌙 Moonwalk Browser Bridge — Chrome Extension

## Quick Install (2 minutes)

### Step 1: Unzip
You've already done this! This folder contains the extension.

### Step 2: Open Chrome Extensions
1. Open Google Chrome
2. Type \`chrome://extensions\` in the address bar and press Enter
3. Turn ON "Developer mode" (toggle in the top-right corner)

### Step 3: Install the Extension
1. Click the "Load unpacked" button (top-left)
2. Select THIS folder (the one containing this README)
3. The "Moonwalk Browser Bridge" extension will appear in your list

### Step 4: Pin the Extension
1. Click the puzzle piece icon 🧩 in Chrome's toolbar
2. Click the pin 📌 next to "Moonwalk Browser Bridge"

### Step 5: Connect to Moonwalk
The extension connects automatically to the Moonwalk desktop app.
If you need to change settings:
1. Right-click the Moonwalk extension icon → "Options"
2. The default Bridge URL is \`ws://127.0.0.1:8765\` (local mode)
3. Click "Save Settings"

### Troubleshooting
- **Extension not connecting?** Make sure the Moonwalk desktop app is running
- **"Developer mode" warning?** This is normal for extensions not from the Chrome Web Store. Click "Dismiss" each time Chrome starts.
- **Need help?** Contact support@moonwalk.ai

---
*Moonwalk Browser Bridge v1.0.0*
`;

writeFileSync(join(STAGING, "INSTALL.md"), readmeContent);
console.log("📝 Added INSTALL.md guide");

// ── Create zip ──
const zipPath = join(DIST, "moonwalk-browser-bridge.zip");
if (existsSync(zipPath)) {
  execSync(`rm "${zipPath}"`);
}

execSync(`cd "${DIST}" && zip -r "${zipPath}" "moonwalk-browser-bridge/"`, {
  stdio: "inherit",
});

console.log(`\n✅ Extension packaged: ${zipPath}`);
console.log(`   Size: ${(execSync(`stat -f%z "${zipPath}"`, { encoding: "utf8" }).trim() / 1024).toFixed(0)} KB\n`);

// ── Clean staging ──
execSync(`rm -rf "${STAGING}"`);
