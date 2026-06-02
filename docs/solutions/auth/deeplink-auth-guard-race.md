# Deep-link / refresh of a protected route bounces to /dashboard (MSAL-init auth-guard race)

**Date:** 2026-06-02
**Branch:** `fix/broker-accounts-deeplink-auth-race`
**Surface:** frontend auth shell (`frontend/src/components/layout/app-shell.tsx`) — high-impact (authentication/routing)
**Symptom severity:** P1 — every direct-URL load / refresh / bookmark of a non-dashboard protected route silently redirected to `/dashboard`. Broker Accounts was effectively unreachable (no sidebar link → only reachable by direct URL → always raced).

---

## Symptom

On **prod** (`platform.marketsignal.ai`), authenticated as the operator:

- Full-page load of `https://platform.marketsignal.ai/settings/broker-accounts` → ends on `/dashboard`.
- Full-page load of `/strategies` → ends on `/dashboard`.
- In-app **sidebar** (SPA) navigation to the same pages → stays put (no bounce).

Reproduced deterministically via Playwright driving the operator's authenticated Chrome session.

Dev never reproduced it (see "Why dev masks it").

## Root cause

`app-shell.tsx` decided auth from `useIsAuthenticated()` **alone**:

```tsx
const isAuthenticated = useIsAuthenticated();
// ...
useEffect(() => {
  if (!isAuthenticated && !isPublicRoute) router.replace("/login");
  if (isAuthenticated && isPublicRoute) router.replace("/dashboard");
}, [isAuthenticated, isPublicRoute, router]);
```

On a **full page load**, MSAL (`@azure/msal-react` v5) is mid-`initialize()` + `handleRedirectPromise()`, so `useIsAuthenticated()` is briefly **false** before the cached account rehydrates. The sequence:

1. Direct load of `/settings/broker-accounts` → `isAuthenticated` momentarily `false` → effect fires `router.replace("/login")`.
2. MSAL finishes rehydrating → now authenticated, current route is `/login` → effect's second branch fires `router.replace("/dashboard")`.
3. Net result: every direct-URL load of a protected route lands on `/dashboard`.

In-app navigation didn't race because MSAL was already initialized by then.

Broker Accounts was uniquely exposed because it had **no sidebar nav link** (shipped in PR #86 without one) — so it could only be opened by direct URL, which always full-loads, which always raced. The app-shell bug itself was **pre-existing**; PR #86 merely surfaced it via a page with no in-app entry point.

## Why dev masks it

The dev stack forces `isAuthenticated = true` via the auth-bypass — `NODE_ENV === "development"` **or** `NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"` **or** a non-empty `NEXT_PUBLIC_MSAI_API_KEY` (centralized as `isAuthBypassed()` in `frontend/src/lib/auth.ts`). With the bypass on, there is no MSAL init window to race, so the bounce never happens on dev. This is why unit/integration/dev-E2E could not reproduce it — the race only exists where real MSAL initialization runs (prod / any non-bypassed build).

## Fix

Two files:

1. **`app-shell.tsx`** — gate the redirect on MSAL having **settled**, using the canonical MSAL v5 idiom (`useMsal().inProgress === InteractionStatus.None`, confirmed via Context7):

   ```tsx
   const isBypassed = isAuthBypassed();
   const isAuthenticated = isBypassed ? true : useIsAuthenticated();
   const { inProgress } = useMsal();
   const msalSettled = isBypassed || inProgress === InteractionStatus.None;

   useEffect(() => {
     if (!msalSettled) return; // don't act on the init-window false
     if (!isAuthenticated && !isPublicRoute) router.replace("/login");
     if (isAuthenticated && isPublicRoute) router.replace("/dashboard");
   }, [msalSettled, isAuthenticated, isPublicRoute, router]);

   // Hold a spinner (not a render-then-bounce) until MSAL settles AND auth resolves.
   if (!msalSettled || !isAuthenticated) return <Spinner label="Loading..." />;
   ```

   Safe because `providers.tsx` always mounts `<MsalProvider>` after `msalInstance.initialize()`, so `useMsal()` is valid here. The bypass short-circuits the wait so dev/E2E render exactly as before (behavior-preserving on dev).

2. **`sidebar.tsx`** — add a "Broker Accounts" nav item (`/settings/broker-accounts`, between System and Settings) so the page is reachable in-app, AND a **longest-prefix `activeHref`** so `/settings/broker-accounts` highlights only "Broker Accounts" and not also "Settings" (a naive `pathname.startsWith(href)` would mark both active).

## Verification

- **Dev E2E** (`tests/e2e/use-cases/auth/deeplink-auth-race.md`, spec `frontend/tests/e2e/specs/deeplink-auth-race.spec.ts`): UC-DLA-1 (deep-link lands + reload) + UC-DLA-2 (sidebar nav + only-Broker-Accounts-active) both PASS. Dev proves sidebar reachability + active-state + that the new settle-gate does not regress the dev render path.
- **The race fix itself** is intrinsically not dev-reproducible (bypass) → proven by (a) Codex + pr-toolkit review of the `inProgress` gate against the MsalProvider lifecycle, and (b) the **post-deploy prod re-test**: re-run the exact reproduction (authenticated full-load of `/settings/broker-accounts` must land on the page, not `/dashboard`).

## Lessons

- An auth guard that reads `useIsAuthenticated()` without also gating on `inProgress === InteractionStatus.None` is racy on every full page load. Gate redirects on MSAL having **settled**, not just on the current authenticated boolean.
- A dev auth-bypass that forces `isAuthenticated=true` hides this entire class of init-race bug from every non-prod test layer. For auth-shell changes, the closing gate is a **prod (non-bypassed) re-test**, not dev E2E.
- Shipping a protected page with **no in-app nav entry** turns a latent full-load race into a 100%-reproducible outage for that page — always give a protected route a sidebar/menu entry so it isn't direct-URL-only.

## Related

- Companion auth lesson: `feedback_msal_scope_must_target_backend_audience.md` (MSAL scope must target the backend audience) — same MSAL surface, different failure mode.
- PR #86 (`broker-account-entity`) — the page whose missing nav link exposed this pre-existing race.
