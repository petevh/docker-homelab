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
**Fork fix:** `feat/web-native-detection` @ `e534e72d8` — `hooks/useMicrosoftAuth.ts`

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

<!-- Add further issues below in the same format. -->
