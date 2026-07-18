# IntuneGet — Self-Hosting Design Note

> **Styled version:** <https://claude.ai/code/artifact/9f391523-0e44-4268-9ae0-49cfddd87182>
> (private Claude artifact — same content, nicer to read)

Where the self-hosted IntuneGet work landed, the architecture to build next, and
the assumptions that turned out false. Written to be picked up cold.

- **Tenant:** Kemyion (single admin)
- **Instance:** intuneget.app.vanheerden.ch
- **Pinned:** upstream `v0.7.1` (clone at `/mnt/development/IntuneGet`)
- **Status:** catalog live · packaging redesign pending
- **Verified at:** `v0.7.1` / catalog snapshot `2026-07-10`. Re-check line numbers,
  versions, and the Store-app list if either moves.

---

## 1. The principle underneath everything

Every hard call reduces to one rule, applied at different layers:

> **Package an app with the tool whose native detection and lifecycle match how the
> app is actually distributed — and prefer mature, purpose-built code over a single
> project's custom implementation.**

Applied downward it decides three things at once:

- **Store app** → Intune's native *Microsoft Store app (new)*. Microsoft owns
  detection and updates. Do not package it.
- **Classic Win32 installer** (no Store presence) → the real gap. Something must
  fetch the installer, build a `.intunewin`, and supply a detection rule.
- **Within Win32** → use the mature builder (IntuneWin32App), not IntuneGet's custom
  packager. Use IntuneGet only for the catalog.

---

## 2. Target architecture

Split the pipeline at the one genuine seam — the boundary between "needs Windows"
and "needs the Kemyion secret." The `.intunewin` is portable bytes; only *building*
it needs Windows, only *uploading* it needs credentials. They never have to co-locate.

| Stage | Where | Does | Secret? |
|---|---|---|---|
| **Discovery** | IntuneGet catalog (existing container) | package identity, installer URL, `installer_type`, `silent_args` when present | no |
| **Build** (needs Windows) | GitHub Actions Windows runner, on-demand | fetch installer → `IntuneWinAppUtil.exe` → `.intunewin` + encryption metadata | **no** |
| **Upload** (needs the secret) | the container | pull artifact from GitHub → Microsoft Graph upload with Kemyion creds | yes, stays local |

**Handoff contract:** the `.intunewin` **plus** its extracted `encryptionInfo` (AES
key + MAC, read from the file's internal `detection.xml` at build time — Graph needs
it to decrypt server-side).

**Windows execution — open decision:** on-demand GitHub runner (zero idle infra) vs.
a wake-on-demand Proxmox VM (fully self-hosted, no third party). Decide on real
packaging cadence, not a guessed one. Polling (not webhooks) is correct either way —
the packager reaches *out*, so no inbound ports; see the network reasoning in the
session that produced this.

### Build tool — leaning IntuneWin32App, not decided

[IntuneWin32App](https://github.com/MSEndpointMgr/IntuneWin32App) (MSEndpointMgr) is
the community-standard PowerShell module. It packages the **raw installer** and lets
you set the install command yourself — **no PSADT wrapper**. IntuneGet instead wraps
every package in PSAppDeployToolkit and hardcodes `Invoke-AppDeployToolkit.exe` as
the entry point.

For a single admin who doesn't need PSADT's deferrals / user-close / rich logging,
the raw path is simpler and drops the custom pipeline entirely.

**Genuine counter-consideration:** you have *already fixed* IntuneGet's upload code
(`intune-uploader.ts`) — five Graph-payload bugs, on the fork branch. Reusing your
own known-good upload code may now beat adopting the module. This changed the moment
that fix landed. Weigh "reuse fixed fork code" vs. "adopt mature module" when building.

---

## 3. The silent-args gap

Dropping PSADT means one small piece of knowledge lands on you. The catalog carries
native silent switches — but only *sometimes* (sampled from the live snapshot):

| App | `installer_type` | `silent_args` |
|---|---|---|
| 7-Zip | `exe` | `/S` |
| Firefox | `nullsoft` | `/S /PreventRebootRequired=true` |
| Chrome | `wix` | `null` |
| Notepad++ | `nullsoft` | `null` |
| VLC | `nullsoft` | `null` |

IntuneGet gets away with the nulls because PSADT (and type conventions) fill them in.
Without PSADT, **you own a fallback: `installer_type` → default silent switch.**

```
# used only when silent_args is null
wix | msi   →  msiexec /qn
nullsoft    →  /S
inno        →  /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
exe         →  (no universal default — verify per app)
```

> **Watch:** a wrong `exe` silent switch means an installer that hangs waiting for a
> click on an unattended machine. The table covers common types; bare `exe` still
> needs a per-app check.

---

## 4. Store apps — the boundary can't be fully automated

The catalog tags apps by source, which *looks* like a clean gate. It isn't.

- **Native, for genuine Store apps:** Intune → Apps → *Microsoft Store app (new)*.
  Native detection, Store-driven updates, built-in monitoring, zero exposure to
  third-party packaging bugs. Strictly better whenever it applies.
- **Don't trust `app_source`:** only **11** apps are tagged `store` — almost all
  Microsoft's own (Company Portal, To Do, Whiteboard, Snipping Tool, WhatsApp, …).
  **1Password is tagged `win32` despite being a real Store app**, so IntuneGet
  packaged it as Win32. The field means "how winget sourced it," not "is it in the
  Store."

**Practical rule: check the Store first, by hand, for any given app.** The
`app_source = 'win32'` filter is a weak safety net — catches the 11 obvious cases,
misses exactly the third-party apps most likely to trip you up. Automating this
properly means querying the Store API by name (a separate lookup the catalog can't
stand in for) — worth building only if the manual check becomes a burden.

---

## 5. Catalog sync — the other half, still open

The packaging redesign does **not** fix the failure that started this. Browsing is
stale because **upstream stopped publishing catalog snapshots on 2026-07-10.** These
are independent problems.

What broke Chrome: the snapshot pins `150.0.7871.115`; winget-pkgs moved to `.125`
and **deleted** the old directory, so the live manifest fetch 404s → "No installers
found." Your instance already holds the newest snapshot that exists — nothing local
fixes it. It's a live dependency on one maintainer's release cadence.

> **Storage — decided: keep SQLite, do not add Postgres to the runtime.** The catalog
> is read-only public data replaced wholesale on sync — SQLite's strength (single
> file, atomic swap, no server). The app has no generic Postgres adapter anyway; only
> `sqlite` and Supabase-over-REST. Postgres would add a stateful service *and* custom
> adapter code to replace a file that already works. Whatever populates the catalog,
> the output stays a `catalog.sqlite` the container downloads.

### Three ways to own the populator

1. **Fork the workflows to your own Supabase.** Reuse upstream's `build-app-list` +
   `sync-manifests` on your fork's schedule, publish to your own release. Least code.
   Reintroduces a build-time Supabase (public catalog data only — never Kemyion).
   Needs the DB schema bootstrapped; check the free tier fits ~13.5k apps / ~37k
   version rows.
2. **Write a Supabase-free harvester.** svrooij's winget index (or winget-pkgs
   directly) → straight into local SQLite in the snapshot schema → gzip + manifest.
   Most self-contained, no external DB. Most new code, and it must track the snapshot
   schema or the instance breaks silently.
3. **Point at your own release, decide later.** Set `CATALOG_SNAPSHOT_BASE_URL` /
   `CATALOG_SNAPSHOT_FILE` now to control the source; build one snapshot by hand
   first to prove the instance reads it, then choose the automation.

**Dependency chain, honestly:** `winget-pkgs → svrooij's index → (a Postgres/Supabase)
→ snapshot release → your instance`. No version has zero dependencies — winget data
is inherently someone else's. The goal is to depend on *stable, purpose-built*
upstreams (Microsoft, arguably svrooij), not on one hobbyist project's release
cadence — the link that just failed.

---

## 6. Traps we hit — the through-line

Every one was a field/setting that *looked* authoritative but answered a different
question than the one that mattered. Checking, not trusting, was correct every time.

| Signal | Looked like | Actually meant |
|---|---|---|
| `DATABASE_MODE` | picks the catalog source | doesn't — source is `isSupabaseConfigured()`. Two switches that look like one. |
| `latest_version` | the version to package | points at a winget-pkgs version deleted upstream → 404 |
| `app_source` | "is it a Store app?" | "how did winget source it?" — 1Password is `win32` yet a real Store app |
| redirect URI | bare domain (per upstream docs) | MSAL hardcodes `/redirect` and `/auth/consent-callback`; docs wrong |
| `/data` volume | writable by the app | root-owned; non-root `nextjs` couldn't write. Health lied "healthy" |
| "recently maintained" | the packager is cared for | recent commits never touched the core payload — broken since first release |

> **Load-bearing caveat:** the upload code (`intune-uploader.ts`) is fixed on the fork
> branch `fix/packager-win32lobapp-create-payload` — **not** in the pinned `v0.7.1`
> clone, which still ships the five-bug payload. If you reuse the upload half, lift it
> from the fork, not the clone.

---

## 7. Where things stand

| State | Item |
|---|---|
| **Done** | Web app live — catalog browsing, TLS via Traefik, MSAL sign-in verified, no Supabase, secret local. On `main`, pushed. |
| **Done** | Packager payload fixed — five Graph-payload bugs, on the fork branch. Deploys succeed against a live tenant. |
| **Done** | `PACKAGER_API_KEY` in 1Password. |
| **Done** | Snapshot override wired (§5 opt 3) — `CATALOG_SNAPSHOT_BASE_URL/_FILE/_DIR` documented in compose + `.env.example`. |
| **Done (superseded)** | Prove-it: Chrome fixed via a hand-built catalog (`scripts/patch-catalog-chrome.mjs`). Now replaced by the full populator below, but the script stays as the minimal single-app fix pattern. `CATALOG_SNAPSHOT_FILE=/data/catalog.local.sqlite` (skips all snapshot networking); built by `scripts/patch-catalog-chrome.mjs` (clone frozen snapshot → bump Chrome to the live winget-pkgs version, real per-arch SHAs). Verified through the app: `/api/winget/search` and `/api/winget/manifest` both return `150.0.7871.129` with 3 installers. **Caveat:** the `.local.sqlite` lives on the (un-tracked) `/data` volume — re-run the script after a fresh volume, and when winget-pkgs rolls Chrome. This proves the override path; it is NOT the populator. |
| **Open** | Packaging redesign — build on GitHub runner, upload in container, IntuneWin32App vs. fork's upload code (§2), plus silent-args fallback (§3). |
| **Done** | Catalog populator built (§5 opt 2) — `scripts/build-catalog.mjs`, a MERGE harvester: base = frozen snapshot (keeps curation: 13.5k categories, 12.7k icons, descriptions), overlay = svrooij's winget-pkgs-index v2 (live app list + current version + tags). No Supabase, no external DB. Output = 14,038 apps, one current-version `version_history` row each (installers NULL — packaging fetches them live). Verified through the app: search shows fresh versions (Firefox 152.0.6, VLC 3.0.23, Chrome 150.0.7871.129), 36 categories, live manifests resolve. Supersedes the by-hand prove-it. Includes a **rules-based categorizer**: learns a tag→category confidence map from the frozen catalog's own labeled apps (1,224 rules at share≥0.6/support≥5 — generic tags like `windows`/`electron` self-suppress below the floor), then scores each new app by summing matched-tag confidence and assigns above 0.9. Self-contained (no rules file, no LLM); assigns ~113 of 510 new apps, leaves the tagless/weak ones null rather than guessing. **Still-open bits:** new apps with no/weak tags stay uncategorized (no icons either — icon extraction not reproduced); needs scheduling (cron here or Actions) — currently run by hand. **Correction retained:** packaging resolves the installer *live* from winget-pkgs (`getManifest`), so the catalog only needs identity + current version, not installer bytes. |
| **Open** | Windows execution — on-demand GitHub runner vs. wake-on-demand Proxmox VM. Decide on real cadence. |
