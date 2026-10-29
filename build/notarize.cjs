// ═══════════════════════════════════════════════════════════════
//  Moonwalk — electron-builder afterSign hook
//  Notarizes the macOS .app with Apple's notary service.
//
//  Required environment variables:
//    APPLE_ID              — Your Apple ID email
//    APPLE_APP_PASSWORD    — App-specific password (NOT your Apple ID password)
//    APPLE_TEAM_ID         — Your Apple Developer Team ID
//
//  Generate an app-specific password at: https://appleid.apple.com/account/manage
//  Find your Team ID at: https://developer.apple.com/account → Membership
// ═══════════════════════════════════════════════════════════════

const { notarize } = require("@electron/notarize");
const { execSync } = require("child_process");

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;

  // Only macOS
  if (electronPlatformName !== "darwin") {
    console.log("[Notarize] Skipping — not a macOS build.");
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  // ── Ad-hoc sign (free, no Apple account) ────────────────────────────────────
  // Without ANY code signature, macOS TCC cannot persist microphone / camera
  // permissions between launches — the user gets prompted on every open.
  // Ad-hoc signing (identity "-") gives the bundle a stable hash-based identity
  // so TCC remembers the grant. It still shows the Gatekeeper warning on first
  // launch (right-click → Open), but mic permission is then remembered forever.
  if (!process.env.APPLE_ID) {
    console.log("[Sign] No Developer ID found — applying ad-hoc signature so TCC persists mic permission...");
    try {
      execSync(
        `codesign --deep --force --sign "-" --entitlements "${__dirname}/entitlements.mac.plist" "${appPath}"`,
        { stdio: "inherit" }
      );
      console.log("[Sign] ✅ Ad-hoc signature applied.");
    } catch (e) {
      console.warn("[Sign] ⚠️  Ad-hoc signing failed (non-fatal):", e.message);
    }
    return; // Skip notarization — ad-hoc signed apps can't be notarized
  }

  // Skip notarization if credentials aren't fully set
  if (!process.env.APPLE_APP_PASSWORD || !process.env.APPLE_TEAM_ID) {
    console.log("[Notarize] Skipping — APPLE_APP_PASSWORD or APPLE_TEAM_ID not set.");
    return;
  }

  console.log(`[Notarize] Submitting ${appPath} to Apple Notary Service...`);

  await notarize({
    appPath,
    appleId: process.env.APPLE_ID,
    appleIdPassword: process.env.APPLE_APP_PASSWORD,
    teamId: process.env.APPLE_TEAM_ID,
  });

  console.log("[Notarize] ✅ Notarization complete!");
};
