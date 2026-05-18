"use client";

import { useIsAuthenticated } from "@azure/msal-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Sidebar } from "./sidebar";
import { Header } from "./header";

const PUBLIC_ROUTES = ["/login"];

export function AppShell({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const _isAuthenticated = useIsAuthenticated();
  // Bypass the MSAL UI gate for the three documented auth modes:
  //   - ``NODE_ENV === "development"`` — local dev loop
  //   - ``NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"`` — Playwright E2E
  //   - ``NEXT_PUBLIC_MSAI_API_KEY`` set — API-key-only sessions
  // Codex iter-2 P2 #3: the API-key bypass was honored by ``useAuth()``
  // but not by this guard, so the documented API-key flow redirected
  // to ``/login`` in production. Mirrors ``isAuthBypassed()`` from
  // ``lib/auth.ts``.
  const isDevBypass = process.env.NODE_ENV === "development";
  const isE2EBypass = process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS === "1";
  const isApiKeyBypass = Boolean(process.env.NEXT_PUBLIC_MSAI_API_KEY);
  const isAuthenticated =
    isDevBypass || isE2EBypass || isApiKeyBypass ? true : _isAuthenticated;
  const pathname = usePathname();
  const router = useRouter();
  const isPublicRoute = PUBLIC_ROUTES.includes(pathname);

  useEffect(() => {
    if (!isAuthenticated && !isPublicRoute) {
      router.replace("/login");
    }
    if (isAuthenticated && isPublicRoute) {
      router.replace("/dashboard");
    }
  }, [isAuthenticated, isPublicRoute, router]);

  // Public routes (login) render without shell
  if (isPublicRoute) {
    return <>{children}</>;
  }

  // Protected routes render with sidebar + header
  if (!isAuthenticated) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="size-8 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
          <p className="text-sm text-muted-foreground">Redirecting...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}
