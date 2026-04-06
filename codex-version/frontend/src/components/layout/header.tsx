"use client";

import { useMemo } from "react";
import { usePathname } from "next/navigation";

import { useAuth } from "@/lib/auth";

function titleFromPath(pathname: string): string {
  if (pathname === "/") return "Dashboard";
  return pathname
    .split("/")
    .filter(Boolean)
    .map((segment) => segment.replace(/-/g, " "))
    .map((segment) => segment[0]?.toUpperCase() + segment.slice(1))
    .join(" / ");
}

export function Header() {
  const pathname = usePathname();
  const { user, isAuthenticated, login, logout } = useAuth();
  const title = useMemo(() => titleFromPath(pathname), [pathname]);

  return (
    <header className="flex items-center justify-between border-b border-white/10 px-4 py-3 md:px-6">
      <div>
        <p className="text-xs uppercase tracking-[0.2em] text-zinc-400">Control Surface</p>
        <h1 className="text-xl font-semibold text-white">{title}</h1>
      </div>
      <div className="flex items-center gap-3">
        {user ? <span className="text-sm text-zinc-300">{user.name}</span> : null}
        {isAuthenticated ? (
          <button
            onClick={() => void logout()}
            className="rounded-md border border-red-300/30 bg-red-500/20 px-3 py-1.5 text-sm text-red-100"
            type="button"
          >
            Logout
          </button>
        ) : (
          <button
            onClick={() => void login()}
            className="rounded-md border border-cyan-300/40 bg-cyan-500/20 px-3 py-1.5 text-sm text-cyan-100"
            type="button"
          >
            Login
          </button>
        )}
      </div>
    </header>
  );
}
