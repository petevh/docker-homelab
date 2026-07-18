/**
 * Self-hosted catalog harvester (DESIGN.md §5, option 2 — the populator).
 *
 * Upstream froze its published catalog snapshot on 2026-07-10. This rebuilds the
 * catalog from live data WITHOUT Supabase, as a MERGE:
 *
 *   base    = the frozen upstream snapshot (curation we want to keep: category,
 *             icon_path, description, homepage, license, popularity_rank, ...)
 *   overlay = svrooij's winget-pkgs-index v2 (live app list + current version + tags)
 *
 * Why a merge and not a from-scratch harvest: winget-pkgs carries NO category or
 * icon, and svrooij's v2 index carries only {Name, PackageId, Version, Tags}. The
 * frozen snapshot already has ~13.5k hand-categorized rows with icons — throwing
 * that away to rebuild from raw manifests would be a browsing regression. So we keep
 * the curation and refresh only the volatile parts.
 *
 * Installers are deliberately NOT harvested: the packaging flow
 * (app/api/winget/manifest/route.ts -> lib/winget-api.ts) fetches the installer LIVE
 * from winget-pkgs at package time. The catalog only needs app identity + which
 * version currently exists, so version_history carries one row per app (current
 * version, installer columns NULL) purely to drive the version selector + latest.
 *
 * The SQLite schema is copied VERBATIM from upstream's scripts/build-catalog-snapshot.mjs
 * (buildSqlite) so the output cannot drift from what the app's SnapshotCatalogSource
 * expects. If upstream bumps SNAPSHOT_SCHEMA_VERSION, re-sync the DDL below.
 *
 * Run INSIDE the intuneget container (has better-sqlite3, node 20, /data volume):
 *   curl -sL https://github.com/svrooij/winget-pkgs-index/raw/main/index.v2.json \
 *     -o /path/idx.json                       # fetch on the host (container has no curl/egress guarantee)
 *   docker cp .../idx.json intuneget:/data/... # or mount; then:
 *   docker exec -i intuneget env SVROOIJ_INDEX=/data/idx.json FROZEN=/data/catalog.frozen.bak \
 *     OUT=/data/catalog.local.sqlite node --input-type=module < intuneget/scripts/build-catalog.mjs
 *
 * Then CATALOG_SNAPSHOT_FILE=/data/catalog.local.sqlite (already set in compose) picks
 * it up on restart; that var skips all snapshot networking so nothing overwrites it.
 */

import Database from 'better-sqlite3';
import { readFileSync } from 'node:fs';

const SCHEMA_VERSION = 1; // must match upstream lib/catalog/snapshot-store.ts SNAPSHOT_SCHEMA_VERSION

const FROZEN = process.env.FROZEN || '/data/catalog.frozen.bak';
const SVROOIJ_INDEX = process.env.SVROOIJ_INDEX || '/data/idx.json';
const OUT = process.env.OUT || '/data/catalog.local.sqlite';

// ---- upstream DDL, verbatim from scripts/build-catalog-snapshot.mjs (buildSqlite) ----
const DDL = `
  CREATE TABLE curated_apps (
    id INTEGER PRIMARY KEY, winget_id TEXT, name TEXT, publisher TEXT,
    latest_version TEXT, description TEXT, homepage TEXT, license TEXT,
    popularity_rank INTEGER, category TEXT, subcategory TEXT, tags TEXT,
    icon_path TEXT, has_icon INTEGER, is_verified INTEGER, is_locale_variant INTEGER,
    parent_winget_id TEXT, locale_code TEXT, app_source TEXT, store_package_id TEXT,
    created_at TEXT
  );
  CREATE INDEX idx_curated_winget ON curated_apps(winget_id);
  CREATE INDEX idx_curated_winget_nocase ON curated_apps(winget_id COLLATE NOCASE);
  CREATE INDEX idx_curated_popular ON curated_apps(is_verified, is_locale_variant, popularity_rank);
  CREATE INDEX idx_curated_category ON curated_apps(category);

  CREATE VIRTUAL TABLE curated_fts USING fts5(name, publisher, description, tags);

  CREATE TABLE version_history (
    winget_id TEXT, version TEXT, installer_url TEXT, installer_sha256 TEXT,
    installer_type TEXT, installer_scope TEXT, silent_args TEXT, installers TEXT,
    created_at TEXT, PRIMARY KEY (winget_id, version)
  );
  CREATE INDEX idx_vh_winget ON version_history(winget_id);

  CREATE TABLE sccm_winget_mappings (
    id TEXT PRIMARY KEY, sccm_display_name_normalized TEXT, sccm_ci_id TEXT,
    sccm_product_code TEXT, winget_package_id TEXT, winget_package_name TEXT,
    confidence REAL, is_verified INTEGER
  );
  CREATE INDEX idx_sccm_name ON sccm_winget_mappings(sccm_display_name_normalized);
  CREATE INDEX idx_sccm_ci ON sccm_winget_mappings(sccm_ci_id);
`;

const bool = (v) => (v ? 1 : 0);

function readFrozen() {
  const db = new Database(FROZEN, { readonly: true });
  try {
    const apps = db.prepare('SELECT * FROM curated_apps').all();
    const sccm = db.prepare('SELECT * FROM sccm_winget_mappings').all();
    return { apps, sccm };
  } finally {
    db.close();
  }
}

function readSvrooij() {
  const raw = JSON.parse(readFileSync(SVROOIJ_INDEX, 'utf8'));
  // key by PackageId, case-insensitively (winget ids are case-insensitive)
  const byId = new Map();
  for (const e of raw) {
    if (!e.PackageId) continue;
    byId.set(e.PackageId.toLowerCase(), e);
  }
  return byId;
}

function main() {
  const { apps: frozenApps, sccm } = readFrozen();
  const live = readSvrooij();
  console.log(`frozen curated_apps: ${frozenApps.length}`);
  console.log(`svrooij live packages: ${live.size}`);

  const now = new Date().toISOString();
  const curatedApps = [];
  const versionHistory = [];
  const seen = new Set();
  let maxId = 0;
  let refreshed = 0;
  let keptStale = 0;
  let droppedVariant = 0;

  // 1) Walk the frozen catalog. Refresh from live where present; keep curation always.
  for (const a of frozenApps) {
    maxId = Math.max(maxId, a.id);
    const liveEntry = live.get(a.winget_id.toLowerCase());

    if (!liveEntry) {
      // Not in the live index. Drop locale variants (winget no longer carries them);
      // keep real apps conservatively (svrooij may momentarily omit a package).
      if (a.is_locale_variant) {
        droppedVariant++;
        continue;
      }
      keptStale++;
      curatedApps.push(a); // unchanged, including its (possibly stale) latest_version
      seen.add(a.winget_id.toLowerCase());
      versionHistory.push({ winget_id: a.winget_id, version: a.latest_version, created_at: a.created_at || now });
      continue;
    }

    // Present live: keep curation, refresh name/version/tags.
    refreshed++;
    const tags = Array.isArray(liveEntry.Tags) ? liveEntry.Tags : null;
    curatedApps.push({
      ...a,
      name: liveEntry.Name || a.name,
      latest_version: liveEntry.Version || a.latest_version,
      tags: tags ? JSON.stringify(tags) : a.tags,
      is_verified: 1,
    });
    seen.add(a.winget_id.toLowerCase());
    versionHistory.push({ winget_id: a.winget_id, version: liveEntry.Version || a.latest_version, created_at: now });
  }

  // 2) Add NEW apps present in live but not the frozen catalog (no curation yet).
  let added = 0;
  for (const [, e] of live) {
    if (seen.has(e.PackageId.toLowerCase())) continue;
    added++;
    const id = ++maxId;
    const tags = Array.isArray(e.Tags) ? e.Tags : [];
    curatedApps.push({
      id,
      winget_id: e.PackageId,
      name: e.Name || e.PackageId,
      publisher: e.PackageId.includes('.') ? e.PackageId.split('.')[0] : null,
      latest_version: e.Version || null,
      description: null, homepage: null, license: null,
      popularity_rank: null,
      category: null, subcategory: null,
      tags: JSON.stringify(tags),
      icon_path: null, has_icon: 0,
      is_verified: 1, is_locale_variant: 0,
      parent_winget_id: null, locale_code: null,
      app_source: 'win32', store_package_id: null,
      created_at: now,
    });
    versionHistory.push({ winget_id: e.PackageId, version: e.Version || null, created_at: now });
  }

  console.log(`refreshed: ${refreshed} | kept-stale (not in live, non-variant): ${keptStale} | dropped locale variants: ${droppedVariant} | new apps added: ${added}`);
  console.log(`final curated_apps: ${curatedApps.length} | version_history: ${versionHistory.length}`);

  // 3) Build the SQLite file with the verbatim upstream schema.
  const db = new Database(OUT);
  try {
    db.pragma('journal_mode = DELETE');
    db.exec(DDL);

    const insCurated = db.prepare(`INSERT INTO curated_apps
      (id, winget_id, name, publisher, latest_version, description, homepage, license,
       popularity_rank, category, subcategory, tags, icon_path, has_icon, is_verified,
       is_locale_variant, parent_winget_id, locale_code, app_source, store_package_id, created_at)
      VALUES (@id,@winget_id,@name,@publisher,@latest_version,@description,@homepage,@license,
       @popularity_rank,@category,@subcategory,@tags,@icon_path,@has_icon,@is_verified,
       @is_locale_variant,@parent_winget_id,@locale_code,@app_source,@store_package_id,@created_at)`);
    const insFts = db.prepare(`INSERT INTO curated_fts (rowid, name, publisher, description, tags)
      VALUES (@id, @name, @publisher, @description, @tags)`);
    const insVersion = db.prepare(`INSERT OR IGNORE INTO version_history
      (winget_id, version, installer_url, installer_sha256, installer_type, installer_scope,
       silent_args, installers, created_at)
      VALUES (@winget_id,@version,NULL,NULL,NULL,NULL,NULL,NULL,@created_at)`);
    const insSccm = db.prepare(`INSERT OR IGNORE INTO sccm_winget_mappings
      (id, sccm_display_name_normalized, sccm_ci_id, sccm_product_code, winget_package_id,
       winget_package_name, confidence, is_verified)
      VALUES (@id,@sccm_display_name_normalized,@sccm_ci_id,@sccm_product_code,@winget_package_id,
       @winget_package_name,@confidence,@is_verified)`);

    const tx = db.transaction(() => {
      for (const a of curatedApps) {
        insCurated.run({
          id: a.id, winget_id: a.winget_id, name: a.name, publisher: a.publisher ?? null,
          latest_version: a.latest_version ?? null, description: a.description ?? null,
          homepage: a.homepage ?? null, license: a.license ?? null,
          popularity_rank: a.popularity_rank ?? null, category: a.category ?? null,
          subcategory: a.subcategory ?? null, tags: a.tags ?? null, icon_path: a.icon_path ?? null,
          has_icon: bool(a.has_icon), is_verified: bool(a.is_verified),
          is_locale_variant: bool(a.is_locale_variant), parent_winget_id: a.parent_winget_id ?? null,
          locale_code: a.locale_code ?? null, app_source: a.app_source ?? null,
          store_package_id: a.store_package_id ?? null, created_at: a.created_at ?? now,
        });
        // FTS tags column wants space-joined terms, not JSON.
        let ftsTags = '';
        if (a.tags) { try { const t = JSON.parse(a.tags); if (Array.isArray(t)) ftsTags = t.join(' '); } catch {} }
        insFts.run({
          id: a.id, name: a.name ?? '', publisher: a.publisher ?? '',
          description: a.description ?? '', tags: ftsTags,
        });
      }
      for (const v of versionHistory) {
        if (v.version == null) continue; // skip apps svrooij listed with no version
        insVersion.run({ winget_id: v.winget_id, version: v.version, created_at: v.created_at ?? now });
      }
      for (const m of sccm) {
        insSccm.run({
          id: m.id, sccm_display_name_normalized: m.sccm_display_name_normalized ?? null,
          sccm_ci_id: m.sccm_ci_id ?? null, sccm_product_code: m.sccm_product_code ?? null,
          winget_package_id: m.winget_package_id ?? null, winget_package_name: m.winget_package_name ?? null,
          confidence: m.confidence ?? null, is_verified: bool(m.is_verified),
        });
      }
    });
    tx();

    db.exec("INSERT INTO curated_fts(curated_fts) VALUES('optimize');");
    db.pragma('wal_checkpoint(TRUNCATE)');

    const counts = {
      curated_apps: db.prepare('SELECT count(*) c FROM curated_apps').get().c,
      version_history: db.prepare('SELECT count(*) c FROM version_history').get().c,
      sccm_winget_mappings: db.prepare('SELECT count(*) c FROM sccm_winget_mappings').get().c,
      schemaVersion: SCHEMA_VERSION,
    };
    console.log('WROTE', OUT, JSON.stringify(counts));
  } finally {
    db.close();
  }
}

main();
