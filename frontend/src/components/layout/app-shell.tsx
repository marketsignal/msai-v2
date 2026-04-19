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
  // DEV BYPASS: skip auth for local testing
  const isAuthenticated =
    process.env.NODE_ENV === "development" ? true : _isAuthenticated;
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
