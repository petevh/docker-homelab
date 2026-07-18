/**
 * Prove-it catalog patch (DESIGN.md §5, option 3).
 *
 * Upstream froze its catalog snapshot on 2026-07-10. The frozen snapshot pins
 * Google Chrome at 150.0.7871.115 — a winget-pkgs version directory Microsoft has
 * since DELETED. The packaging flow (app/api/winget/manifest/route.ts) resolves the
 * version to package from the catalog's version_history (newest first) and then
 * fetches that version's manifest LIVE from winget-pkgs. With the frozen snapshot
 * that live fetch 404s -> "No installers found".
 *
 * winget-pkgs keeps exactly ONE numeric Chrome directory live at a time; as of the
 * build below that is 150.0.7871.129. This script clones the frozen snapshot and:
 *   - sets curated_apps.latest_version for Google.Chrome (id=2) to the live version
 *   - inserts a version_history row for the live version (real per-arch SHA256s from
 *     the live manifest), with the newest created_at so getVersions() returns it first
 * so the live manifest fetch now resolves, and Chrome packages again.
 *
 * The output catalog.sqlite is what CATALOG_SNAPSHOT_FILE points at. It uses the
 * upstream schema verbatim (we only INSERT/UPDATE rows), so it cannot drift.
 *
 * Run INSIDE the intuneget container (it has better-sqlite3 + the /data volume):
 *   docker cp intuneget/scripts/patch-catalog-chrome.mjs intuneget:/tmp/patch.mjs
 *   docker exec intuneget node /tmp/patch.mjs
 * Then set CATALOG_SNAPSHOT_FILE=/data/catalog.sqlite in the compose and restart.
 *
 * Re-run when winget-pkgs rolls Chrome to a new version: update LIVE_VERSION and the
 * three SHA256s below from
 *   https://raw.githubusercontent.com/microsoft/winget-pkgs/master/manifests/g/Google/Chrome/<ver>/Google.Chrome.installer.yaml
 */

import Database from 'better-sqlite3';
import { copyFileSync, existsSync } from 'node:fs';

const SRC = process.env.CATALOG_SRC || '/data/catalog.sqlite';
const OUT = process.env.CATALOG_OUT || '/data/catalog.patched.sqlite';

// Live-verified from winget-pkgs (Google.Chrome.installer.yaml). Update on version roll.
const LIVE_VERSION = '150.0.7871.129';
const INSTALLERS = [
  {
    Architecture: 'x64',
    InstallerUrl: 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi',
    InstallerSha256: '8AA9863B32E9BA413ACB1ACA9B50AF11660B368C10802A54588DD00F7A6BC307',
    ProductCode: '{7F0F0C51-16CA-3ED0-BF6C-435E5985D8BC}',
    AppsAndFeaturesEntries: [
      { ProductCode: '{7F0F0C51-16CA-3ED0-BF6C-435E5985D8BC}', UpgradeCode: '{C1DFDF69-5945-32F2-A35E-EE94C99C7CF4}' },
    ],
  },
  {
    Architecture: 'arm64',
    InstallerUrl: 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise_arm64.msi',
    InstallerSha256: '26A57F0A7E50A6D0BE1E604029B41F0F083101BD94DFD453FD1ED8B5FEB29AF8',
    ProductCode: '{B3BEC0D5-F03A-37EA-AD5B-7EF9F5B30E86}',
    AppsAndFeaturesEntries: [
      { ProductCode: '{B3BEC0D5-F03A-37EA-AD5B-7EF9F5B30E86}', UpgradeCode: '{C1DFDF69-5945-32F2-A35E-EE94C99C7CF4}' },
    ],
  },
  {
    Architecture: 'x86',
    InstallerUrl: 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise.msi',
    InstallerSha256: 'E8B02C13FFEF934F752940A89F28CD3C2B5DD17FF102979EE27F0760A6984D1A',
    ProductCode: '{5AD8FE6A-6254-3C41-9AE5-B24C1C0E0E95}',
    AppsAndFeaturesEntries: [
      { ProductCode: '{5AD8FE6A-6254-3C41-9AE5-B24C1C0E0E95}', UpgradeCode: '{C1DFDF69-5945-32F2-A35E-EE94C99C7CF4}' },
    ],
  },
];
const WINGET_ID = 'Google.Chrome';
const X64_SHA = INSTALLERS[0].InstallerSha256;

if (!existsSync(SRC)) throw new Error(`source catalog not found: ${SRC}`);
copyFileSync(SRC, OUT);

const db = new Database(OUT);
try {
  const app = db.prepare('SELECT id, latest_version FROM curated_apps WHERE winget_id = ?').get(WINGET_ID);
  if (!app) throw new Error(`${WINGET_ID} not present in curated_apps`);
  console.log(`curated_apps: ${WINGET_ID} (id=${app.id}) latest_version ${app.latest_version} -> ${LIVE_VERSION}`);

  const now = new Date().toISOString();
  // Match the snapshot's existing encoding: installers is a JSON *string* (double-encoded).
  const installersEncoded = JSON.stringify(JSON.stringify(INSTALLERS));

  const tx = db.transaction(() => {
    db.prepare('UPDATE curated_apps SET latest_version = ? WHERE id = ?').run(LIVE_VERSION, app.id);
    db.prepare(
      `INSERT OR REPLACE INTO version_history
        (winget_id, version, installer_url, installer_sha256, installer_type, installer_scope, silent_args, installers, created_at)
       VALUES (?, ?, ?, ?, 'wix', 'machine', NULL, ?, ?)`
    ).run(WINGET_ID, LIVE_VERSION, INSTALLERS[0].InstallerUrl, X64_SHA, installersEncoded, now);
  });
  tx();

  db.exec("INSERT INTO curated_fts(curated_fts) VALUES('optimize');");
  db.pragma('wal_checkpoint(TRUNCATE)');

  const check = db
    .prepare('SELECT version, created_at FROM version_history WHERE winget_id = ? ORDER BY created_at DESC LIMIT 1')
    .get(WINGET_ID);
  console.log(`version_history newest for ${WINGET_ID}: ${check.version} @ ${check.created_at}`);
  if (check.version !== LIVE_VERSION) throw new Error('newest version_history row is not the live version');
  console.log(`OK. Patched catalog written to ${OUT}`);
  console.log(`To activate: mv ${OUT} /data/catalog.sqlite  (or point CATALOG_SNAPSHOT_FILE at ${OUT})`);
} finally {
  db.close();
}
