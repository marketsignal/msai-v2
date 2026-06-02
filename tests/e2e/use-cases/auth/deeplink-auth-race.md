# Deep-link / refresh auth-guard race — protected-route reachability

> Graduated from `/fix-bug broker-accounts-deeplink-auth-race` (2026-06-02).
> Subject: `frontend/src/components/layout/app-shell.tsx` (MSAL-settle gate) +
> `frontend/src/components/layout/sidebar.tsx` (Broker Accounts nav + longest-prefix active-state).
>
> **Verification note:** the dev stack forces `isAuthenticated=true` via the
> `NODE_ENV=development` / `NEXT_PUBLIC_E2E_AUTH_BYPASS` / API-key bypass
> (`isAuthBypassed()` in `frontend/src/lib/auth.ts`), so dev **cannot reproduce
> the MSAL-initialization race** that caused the prod bounce. On dev these UCs
> prove (a) the sidebar link reaches the page with correct active-state, and (b)
> the new `msalSettled` spinner-gate does not regress the dev render/persist path.
> The race fix itself is proven by the post-deploy **prod** re-test (authenticated
> full-load of `/settings/broker-accounts` must land on the page, not `/dashboard`).

---

## UC-DLA-1 — Operator opens a deep link to Broker Accounts and lands there

**Interface:** UI
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run — dev; race fix confirmed on prod post-deploy)

**Actor:** Signed-in operator who bookmarked the Broker Accounts settings page.

**Scenario:** They paste the bookmarked `/settings/broker-accounts` URL into a fresh
tab (a full page load, not in-app navigation) to check the fleet before market open.
They expect to land on Broker Accounts — not get silently bounced to the dashboard.

**Intent:** The operator opens a direct link to Broker Accounts and stays there,
across a reload, instead of being redirected away.

**Setup:** Authenticate via the documented dev path (`NEXT_PUBLIC_E2E_AUTH_BYPASS`
fixture + X-API-Key; see `frontend/tests/e2e/fixtures/auth.ts`) at origin
`http://localhost:3300`. Do NOT navigate via the sidebar — the point is the direct
full-page load.

**Steps:**

1. Full-page navigation straight to `http://localhost:3300/settings/broker-accounts`
   (browser address bar / bookmark, not an in-app link).
2. Wait briefly for any client-side redirect to settle.

**Verification:** The URL stays `/settings/broker-accounts` (it does NOT bounce to
`/dashboard` or `/login`); the page reads `h1` "Broker accounts" and the
broker-accounts table or empty-state is shown. The operator can immediately act on
the fleet from this page.

**Persistence:** Reload `/settings/broker-accounts` — still on the page with the same
`h1`, no bounce. (On prod, the same reload after a real MSAL init confirms the race is
gone.)

---

## UC-DLA-2 — Operator reaches Broker Accounts from the sidebar

**Interface:** UI
**Priority:** P1
**Status:** GRADUATED
**Last Result:** PASS (2026-06-02, feature run)

**Actor:** Signed-in operator on the dashboard who has not bookmarked Broker Accounts.

**Scenario:** They want to manage IB broker accounts but don't know the URL. They
expect a sidebar entry that takes them straight there, with the nav correctly
highlighting where they are.

**Intent:** The operator navigates from the sidebar to Broker Accounts and sees that
nav item — and only that one — marked active.

**Setup:** Authenticate via the documented dev path at `http://localhost:3300`. Start
on `/dashboard`. Do NOT deep-link to the page — reaching it via the sidebar is the
action under test.

**Steps:**

1. Load `/dashboard`; locate the "Broker Accounts" item in the sidebar nav (between
   "System" and "Settings").
2. Click it (in-app SPA navigation).

**Verification:** The app navigates to `/settings/broker-accounts`; the page reads
`h1` "Broker accounts" and the table/empty-state is shown. In the sidebar, the
"Broker Accounts" item is active (`bg-accent`) and the "Settings" item is NOT —
confirming the longest-prefix `activeHref` logic (no double-highlight, since
`/settings/broker-accounts` is a path-prefix of neither-vs-`/settings`).

**Persistence:** Reload `/settings/broker-accounts` — still on the page, `h1` "Broker
accounts" still shown, sidebar still highlights only "Broker Accounts". (Shares
UC-DLA-1's reload assertion.)
