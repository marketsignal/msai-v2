"use client";

import { useIsAuthenticated, useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Sidebar } from "./sidebar";
import { Header } from "./header";
import { isAuthBypassed } from "@/lib/auth";
import { AccountScopeProvider } from "@/lib/account-scope";

const PUBLIC_ROUTES = ["/login"];

export function AppShell({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const _isAuthenticated = useIsAuthenticated();
  // The three documented auth-bypass modes (dev / E2E / API-key) live in isAuthBypassed()
  // (lib/auth.ts). Reuse it here so this guard can't drift from the useQuery `enabled`
  // flags that also honor it (Codex iter-2 P2 #3 — the guard previously mirrored the
  // expression inline, which is exactly the drift risk this removes).
  const isBypassed = isAuthBypassed();
  const isAuthenticated = isBypassed ? true : _isAuthenticated;
  // On a full page load MSAL is mid-initialize()/handleRedirectPromise(), so
  // useIsAuthenticated() is briefly false. Gate the redirect on MSAL having SETTLED
  // (inProgress === None) so a deep-link / refresh of a protected route doesn't bounce
  // through /login to /dashboard. Bypass modes skip the wait. (The provider-level
  // initialize() gate in providers.tsx is pre-existing and separate from this guard.)
  const { inProgress } = useMsal();
  const msalSettled = isBypassed || inProgress === InteractionStatus.None;
  const pathname = usePathname();
  const router = useRouter();
  const isPublicRoute = PUBLIC_ROUTES.includes(pathname);

  useEffect(() => {
    if (!msalSettled) return; // wait for MSAL to settle; don't bounce on the init-window false
    if (!isAuthenticated && !isPublicRoute) {
      router.replace("/login");
    }
    if (isAuthenticated && isPublicRoute) {
      router.replace("/dashboard");
    }
  }, [msalSettled, isAuthenticated, isPublicRoute, router]);

  // Public routes (login) render without shell
  if (isPublicRoute) {
    return <>{children}</>;
  }

  // Protected routes: hold the spinner while MSAL is still initializing (so a deep-link /
  // refresh doesn't render-then-bounce) AND while a genuine unauthenticated redirect is in
  // flight. Only render the shell once MSAL has settled AND the user is authenticated.
  if (!msalSettled || !isAuthenticated) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="size-8 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
          <p className="text-sm text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <AccountScopeProvider>
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <div className="flex flex-1 flex-col overflow-hidden">
          <Header />
          <main className="flex-1 overflow-y-auto p-6">{children}</main>
        </div>
      </div>
    </AccountScopeProvider>
  );
}
