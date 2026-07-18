#!/usr/bin/env bash
#
# Refresh the self-hosted IntuneGet catalog (DESIGN.md §5). Cron entry point.
#
# Pipeline, run non-interactively:
#   1. fetch svrooij's winget-pkgs-index v2 on the host (container egress not assumed)
#   2. copy it into the container's /data volume, fix perms (docker cp lands root:root 770)
#   3. run build-catalog.mjs INSIDE the container (it has better-sqlite3 + /data) to
#      merge the frozen curation base with the live index -> /data/catalog.new.sqlite
#   4. sanity-check the result, then atomically swap it into /data/catalog.local.sqlite
#   5. restart the container so it reopens the snapshot (CATALOG_SNAPSHOT_FILE caches the
#      DB handle for the process lifetime — a swapped file is ignored until restart)
#
# Everything is guarded: a failure at any step leaves the currently-live catalog in place
# and exits non-zero (so cron mails the error / the log shows it). It never activates an
# empty or malformed catalog.
#
# Requires: the frozen curation base /data/catalog.frozen.bak (NOT in git — lives on the
# named volume). If it's gone (e.g. fresh volume), this aborts loudly rather than build a
# curation-less catalog.
#
# Install: see the crontab line in this repo's docs; logs to /var/log/intuneget or the
# path in LOG below.
set -euo pipefail

CONTAINER="intuneget"
INDEX_URL="https://github.com/svrooij/winget-pkgs-index/raw/main/index.v2.json"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../docker-homelab/intuneget
BUILD_SCRIPT="$REPO_DIR/scripts/build-catalog.mjs"

FROZEN="/data/catalog.frozen.bak"       # curation base (in container)
LIVE="/data/catalog.local.sqlite"       # what the app serves (CATALOG_SNAPSHOT_FILE)
NEW="/data/catalog.new.sqlite"          # build target
IDX_IN_CONTAINER="/data/idx.json"
MIN_ROWS=10000                          # sanity floor: a healthy catalog is ~14k apps

# Durable copy of the frozen curation base on the NAS share (ZFS, snapshotted). The base
# lives on the VM's LOCAL docker volume (/var/lib/docker/...), NOT the NAS, so a volume
# loss would otherwise destroy it. This seed lets the refresh self-heal after a fresh/lost
# volume.
#
# Provenance: catalog.frozen.bak is byte-identical to upstream's published catalog-latest
# release ASSET (a GitHub release, not committed to git; manifest version 2026-07-10 —
# stale, but upstream is active and the release is fine, we just don't control its refresh
# cadence). It is NOT built from the IntuneGet source (the source reads a snapshot but
# can't manufacture one — it's a render of upstream's Supabase). Recovery if this seed is
# lost = re-download catalog.sqlite.gz + manifest.json from
#   https://github.com/ugurkocde/IntuneGet/releases/download/catalog-latest/
# and gunzip. The NAS seed just removes that external dependency from the recovery path.
SEED="$REPO_DIR/seed/catalog.frozen.bak"

log() { echo "[$(date -Is)] $*"; }
die() { echo "[$(date -Is)] ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found in PATH"
[ -f "$BUILD_SCRIPT" ] || die "build script missing: $BUILD_SCRIPT"
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || die "container '$CONTAINER' is not running"

# The frozen curation base must exist inside the container, or we'd build a catalog with
# no categories/icons and silently degrade browsing. If the volume's copy is gone (fresh
# volume, or someone removed it), restore it from the NAS seed before giving up.
if ! docker exec "$CONTAINER" test -f "$FROZEN"; then
  if [ -f "$SEED" ]; then
    log "frozen base missing in volume; restoring from NAS seed $SEED"
    docker cp "$SEED" "$CONTAINER:$FROZEN"
    docker exec -u 0 "$CONTAINER" chmod a+r "$FROZEN"
    docker exec "$CONTAINER" test -f "$FROZEN" || die "restore from seed failed"
  else
    die "frozen base $FROZEN missing in container AND no NAS seed at $SEED. Aborting; live catalog untouched."
  fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; docker exec "$CONTAINER" rm -f "$IDX_IN_CONTAINER" 2>/dev/null || true' EXIT

log "fetching winget index -> $TMP/idx.json"
curl -fsSL --retry 3 --max-time 120 "$INDEX_URL" -o "$TMP/idx.json" \
  || die "failed to download winget index from $INDEX_URL"
# Cheap validity check: non-trivial size + valid JSON array head.
bytes=$(wc -c < "$TMP/idx.json")
[ "$bytes" -gt 500000 ] || die "downloaded index suspiciously small ($bytes bytes)"
head -c1 "$TMP/idx.json" | grep -q '\[' || die "downloaded index is not a JSON array"
log "index ok ($bytes bytes)"

log "copying index into container"
docker cp "$TMP/idx.json" "$CONTAINER:$IDX_IN_CONTAINER"
docker exec -u 0 "$CONTAINER" chmod a+r "$IDX_IN_CONTAINER"

log "building catalog inside container"
docker exec -i \
  -e SVROOIJ_INDEX="$IDX_IN_CONTAINER" \
  -e FROZEN="$FROZEN" \
  -e OUT="$NEW" \
  "$CONTAINER" node --input-type=module < "$BUILD_SCRIPT" \
  || die "build-catalog.mjs failed; live catalog untouched"

# Sanity-check the freshly built file before it goes live.
rows=$(docker exec -i "$CONTAINER" node --input-type=module <<EOF
import Database from 'better-sqlite3';
const db = new Database('$NEW', { readonly: true });
const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type IN ('table','view') OR name LIKE 'curated_fts%'").all().map(r=>r.name));
for (const t of ['curated_apps','curated_fts','version_history','sccm_winget_mappings'])
  if (!names.has(t)) { console.error('missing table '+t); process.exit(3); }
process.stdout.write(String(db.prepare('SELECT count(*) c FROM curated_apps').get().c));
db.close();
EOF
) || die "new catalog failed structural check; live catalog untouched"

[ "$rows" -ge "$MIN_ROWS" ] || die "new catalog has only $rows apps (< $MIN_ROWS); refusing to activate"
log "new catalog ok: $rows apps"

# Atomic swap + restart so the app reopens the snapshot.
log "activating new catalog and restarting $CONTAINER"
docker exec "$CONTAINER" mv "$NEW" "$LIVE"
(cd "$REPO_DIR" && docker compose restart) || die "restart failed after activation"

# Wait for health so a broken catalog surfaces in the cron log, not silently later.
for _ in $(seq 1 30); do
  s=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)
  [ "$s" = "healthy" ] && { log "container healthy; refresh complete ($rows apps)"; exit 0; }
  sleep 4
done
die "container did not become healthy after restart"
