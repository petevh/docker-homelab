# IntuneGet — upstream issues encountered

Running log of bugs/gaps in [ugurkocde/IntuneGet](https://github.com/ugurkocde/IntuneGet)
that we hit running it self-hosted, with the fix we applied on the fork
(`petevh/IntuneGet`). Kept so we can **report these to upstream** and let the
maintainer fix them, rather than carrying divergence forever.

> Deployment context: single-user, self-hosted, local mode (SQLite catalog, no
> Supabase), single-tenant Entra app in the Kemyion tenant. Some issues may only
> manifest in this mode.

---

## 1. Stale MSAL session → infinite silent-renew loop, no login prompt

**Date:** 2026-07-20
**Severity:** High (app becomes unusable until the user manually clears site data)
**Fork fix:** `feat/web-native-detection` @ `1916a29b8` — `hooks/useMicrosoftAuth.ts`

### Symptom
After the browser session goes stale (expired refresh token/cookie — e.g.
overnight), the dashboard shows:

> **Unable to verify organization setup** — Please check your connection and try again.

The user is **never prompted to log in again**. Works fine in a fresh InPrivate
window (no stale cache). Server side is fully healthy — client-credentials token
issues correctly, has `DeviceManagementApps.ReadWrite.All`, and the Graph
`deviceAppManagement/mobileApps` test call returns 200. So the failure is
entirely client-side.

Browser console floods with:

```
Unsafe attempt to initiate navigation for frame with origin
'https://<host>' from frame with URL
'https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?...'.
The frame attempting navigation of the top-level window is sandboxed, but the
flag of 'allow-top-navigation' or 'allow-top-navigation-by-user-activation' is
not set.
```

Server logs show `POST /api/auth/track-signin` with `authMethod:"silent"`
firing every few seconds in a tight loop.

### Root cause
`acquireTokenSilent` renews tokens in a **hidden, sandboxed iframe**. When the
session is stale, `login.microsoftonline.com` responds with a page that tries to
**top-navigate** to interactive login. The iframe sandbox blocks that
navigation. Critically, this failure does **not** reliably surface as an
`InteractionRequiredAuthError`.

In `hooks/useMicrosoftAuth.ts`, both `refreshToken` and `getAccessToken` only
fell back to interactive auth inside:

```ts
} catch (error) {
  if (error instanceof InteractionRequiredAuthError) {
    // acquireTokenPopup(...)
  }
  return null;   // <-- stale-iframe failures land here
}
```

Because the error wasn't that exact type, the popup fallback never fired and the
function returned `null`. `hooks/useOnboardingStatus.ts` maps a null token to
`errorType = 'network_error'` → the misleading "check your connection" banner →
the component retries → another silent iframe → infinite loop. (The popup
fallback would also have been fragile — popups are commonly blocked.)

### Fix
Fall back to `acquireTokenRedirect` on **any** silent-acquisition failure (not
just `InteractionRequiredAuthError`), in both `refreshToken` and
`getAccessToken`. A full-page redirect escapes the iframe sandbox entirely and
cannot be popup-blocked. Added a module-level `interactiveRedirectInFlight`
guard so concurrent callers don't each fire a redirect (redirect storm). The
existing `/redirect` SPA bridge handles the return leg.

### Reproduction for upstream
1. Sign in normally.
2. Let the MSAL session go stale (or revoke/expire the refresh token).
3. Reload the dashboard → loops on "Unable to verify organization setup" with the
   sandboxed-iframe console errors, and never prompts for re-login.

### Suggested upstream framing
Silent renew should degrade to an interactive **redirect** on any failure, and
the `network_error` mapping in `useOnboardingStatus` is misleading — a token
that needs interaction is not a connectivity problem. Consider surfacing
"session expired, signing you in…" instead of "check your connection."

---

## 2. Silent-switch extraction drops `KEY=VALUE` installer properties (e.g. `ACCEPT_EULA=1`)

**Date:** 2026-07-23
**Severity:** High (client install fails; wrong switches shipped to Intune, so
every device targeted by the app fails to install)
**Fork fix:** packager side **fixed** (`feat/packager-intunewin32app` @ `ff312e1e4`); web side **fixed on the interactive packaging path** (`feat/web-native-detection`), with the auto-update path logged as a separate follow-up — see **Fix status** below.
**Affected files (both upstream/main and fork):**
- `lib/msp/silent-switches.ts` — `extractSilentSwitches` (web app)
- `packager/src/job-processor.ts` — `extractSilentSwitches` (packager)

The **same broken regex is duplicated** in these two independent copies, on two
different branches, reached by two different processes. Both had to be fixed
separately.

### Symptom
Packaging **Microsoft.PowerBI** (Power BI Desktop) succeeds and uploads to
Intune, but the client-side install fails. Windows event log / MSI log:

> Product: Microsoft Power BI Desktop (x64) — EULA has not been accepted while
> executing the installation in reduced UI mode. Please add the flag
> ACCEPT_EULA=1 to the command line.

The generated install command reached the packager **with** `ACCEPT_EULA=1`, but
the switches actually handed to PSADT were only `/quiet /norestart` — the EULA
flag was silently dropped.

### Root cause
The winget manifest is correct. `Microsoft.PowerBI` 2.156.951.0 declares:

```yaml
InstallerType: burn
InstallerSwitches:
  Custom: ACCEPT_EULA=1
```

The web app normalizes this correctly: `normalizeInstaller` +
`appendCustomSwitch` (`lib/manifest-api.ts`) fold `Custom` onto the silent args,
producing `/quiet /norestart ACCEPT_EULA=1`, and `generateInstallCommand`
(`lib/detection-rules.ts`) carries it into the install command. So far correct.

The packager then **re-derives** the switches from that command string via
`extractSilentSwitches`, whose extraction regex only matches tokens that begin
with `/` or `-`:

```js
installCommand.match(/(?:\/\S+|-\S+)(?:\s+(?:\/\S+|-\S+))*/)
```

`ACCEPT_EULA=1` begins with a letter, so it is not captured. The match also
stops at the first non-`/`/`-` token, so given `/quiet /norestart ACCEPT_EULA=1`
it returns `/quiet /norestart` and discards the rest. PSADT then launches the
burn installer without `ACCEPT_EULA=1` → the EULA error above.

This is not Power BI-specific — it affects **any** app whose manifest uses
`Custom` switches (or MSI/burn properties) in `KEY=VALUE` form:
`ACCEPT_EULA=1`, `ALLUSERS=1`, `INSTALLDIR=...`, `TRANSFORMS=...`, etc.

### Secondary defect in the same regex (latent, did not affect Power BI)
When the installer filename contains hyphens, the `-\S+` alternative matches a
filename fragment. For
`"PBIDesktopSetup-2026-07_x64.exe" /quiet /norestart ACCEPT_EULA=1` the regex
returns `-2026-07_x64.exe" /quiet /norestart` — leaking part of the filename
into the argument list. (The `lib/msp` copy strips the quoted path first with
`.replace(/^"[^"]+"\s*/, '')`, so it avoids this case; the packager copy does
not pre-strip and is exposed to it.)

### Reproduction for upstream
1. Package `Microsoft.PowerBI` (Power BI Desktop) — a `burn` installer whose
   manifest declares `InstallerSwitches.Custom: ACCEPT_EULA=1`.
2. Upload to Intune and target a device.
3. Install fails: "EULA has not been accepted … add the flag ACCEPT_EULA=1".
4. Inspect the generated PSADT install step / job `silent_switches`: it contains
   only `/quiet /norestart`; `ACCEPT_EULA=1` is missing.

### Suggested upstream framing / fix
The packager should not re-parse switches out of the install-command string with
a `/`-or-`-` regex at all — it throws away every `KEY=VALUE` property. Preferred:
carry the already-normalized silent args (which correctly include `Custom`)
through to the packager as structured data instead of round-tripping through a
command string. Minimum fix: the extractor must preserve bare `KEY=VALUE` tokens
(and pre-strip the quoted installer path so hyphenated filenames don't leak).

### Fix status (fork)

The two copies were fixed with **different** strategies, because the correct
value is available at different points on each side:

**Packager — `ff312e1e4` (`feat/packager-intunewin32app`).** The packager only
ever receives the flattened `job.install_command` string, so parsing is its only
option. Rather than teach the regex about `KEY=VALUE`, the fix **stops
pattern-matching switches entirely**: strip the leading installer path (quoted,
or one bare token) and treat everything after it as switches verbatim. This also
closes the hyphenated-filename leak. Six unit tests added, including the live
`ACCEPT_EULA=1` and SSMS `--campaign <id>` cases. A second real victim was found
in the process: SSMS (`vs_SSMS.exe --quiet --wait --campaign <id>`) was failing
deterministically with exit 5005 because the campaign id (no leading `/`/`-`) was
truncated to a bare `--campaign`.

_Known residual (packager):_ the unquoted-path branch grabs a single
whitespace-delimited token, so an **unquoted** path containing spaces
(`C:\Program Files\App\setup.exe /S`) would still mis-split. Latent — install
commands are emitted with the path quoted — but not covered by a test.

**Web app — `feat/web-native-detection`.** Fixed on the **interactive
packaging path** (cart → package → upload — the path the Power BI failure was
actually on). Different strategy from the packager, because here the
correctly-normalized `silentArgs` **already exists as a structured field**
(`normalizeInstaller` → `appendCustomSwitch` in `lib/manifest-api.ts`, stored on
the installer as `silentArgs`). The web app was flattening it into
`installCommand` via `generateInstallCommand` and then re-extracting it back out
with the broken regex. Changes:

- Added `resolveSilentArgs(installer)` (exported from `lib/detection-rules.ts`) —
  the single source of truth: `installer.silentArgs || getDefaultSilentArgs(type)`.
  `generateInstallCommand` now calls it (no behaviour change).
- Added a structured `silentArgs` field to `Win32CartItem` (`types/upload.ts`),
  populated at every cart-item builder (`stores/cart-store.ts`,
  `hooks/useQuickAdd.ts`, `hooks/use-bulk-add.ts`, `hooks/use-unmanaged-apps.ts`,
  `components/PackageDetails.tsx`, `components/PackageConfig.tsx`,
  `lib/custom-app.ts`).
- `app/api/package/route.ts` now sends `item.silentArgs` verbatim (extractor kept
  only as a fallback for carts persisted before the field existed).
- `lib/msp/batch-orchestrator.ts`: the two `extractSilentSwitches('', type)` calls
  were only ever a default-switch lookup — now call `getDefaultSilentArgs(type)`.
- **Hardened the extractor itself** (`lib/msp/silent-switches.ts`) for the paths
  that still fall back to it (a user-overridden install command in
  `PackageConfig.tsx`, and legacy carts/policies): strip the leading `msiexec`
  token / installer path and the msiexec `/i|/x|/p` action + target, then take
  the remainder **verbatim** instead of the `/`-or-`-` regex. New unit test file
  `lib/msp/silent-switches.test.ts` (8 cases: `ACCEPT_EULA=1`, `--campaign <id>`,
  hyphenated filename, msiexec `/i`+`/x` property preservation, `-DeploymentType`
  fallback). Full related-suite run stayed green (128 tests).

### Follow-up: auto-update path does not resolve `Custom` switches at all (separate gap)

The **auto-update / default-config path** is _not_ fixed and is a distinct,
deeper problem — not the same drop bug. `buildDefaultDeploymentConfig`
(`lib/update-policies/build-deployment-config.ts`) constructs a
`NormalizedInstaller` **without** `silentArgs` (in local mode the catalog
`version_history` row carries no installer fields), so `generateInstallCommand`
falls through to the per-type default and the manifest's `Custom` switches
(e.g. `ACCEPT_EULA=1`) are **never present** — there is nothing for the extractor
to drop. The `packaging_jobs.silent_switches` column exists in the schema but is
never written, so it can't be used to carry the value forward either. Properly
fixing this requires resolving the manifest's `InstallerSwitches.Custom` at
config-build time (a manifest fetch in the trigger path). `app/api/updates/trigger/route.ts`
therefore still re-derives switches from `installCommand` and remains subject to
the drop for that path; a comment there points here. Deferred by choice — the
interactive path (the reported failure) is fixed; auto-update EULA apps are a
narrower case to be handled when that path gets a manifest fetch.

---

<!-- Add further issues below in the same format. -->
