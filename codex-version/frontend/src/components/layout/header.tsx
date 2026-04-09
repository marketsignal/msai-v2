"use client";

import { useMemo } from "react";
import { usePathname } from "next/navigation";

import { useAuth } from "@/lib/auth";

const PAGE_META: Record<string, { eyebrow: string; subtitle: string }> = {
  "/dashboard": {
    eyebrow: "Desk Overview",
    subtitle: "Capital, risk, and execution in one portfolio view.",
  },
  "/research": {
    eyebrow: "Research Console",
    subtitle: "Run sweeps, compare candidates, and shape portfolio ideas before promotion.",
  },
  "/graduation": {
    eyebrow: "Graduation",
    subtitle: "Govern the promotion path from research candidate to paper and live deployment.",
  },
  "/portfolio": {
    eyebrow: "Portfolio Lab",
    subtitle: "Assemble strategy sleeves, model risk, and queue portfolio backtests.",
  },
  "/live": {
    eyebrow: "Live Trading",
    subtitle: "Monitor streaming state, open risk, and deployment health in real time.",
  },
  "/data": {
    eyebrow: "Data Control",
    subtitle: "Manage daily refresh coverage, storage, and operational alerts.",
  },
};

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
  const { user, isAuthenticated, login, logout, authMode } = useAuth();

  const meta = useMemo(() => {
    const direct = PAGE_META[pathname];
    if (direct) return direct;
    const matched = Object.entries(PAGE_META).find(([prefix]) => pathname.startsWith(`${prefix}/`));
    return matched?.[1] ?? { eyebrow: "Control Surface", subtitle: "API-first operator workspace." };
  }, [pathname]);
  const title = useMemo(() => titleFromPath(pathname), [pathname]);
  const now = useMemo(
    () =>
      new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(new Date()),
    [],
  );

  return (
    <header className="sticky top-0 z-20 border-b border-white/10 bg-[linear-gradient(180deg,rgba(6,9,14,0.88),rgba(6,9,14,0.7))] px-4 py-4 backdrop-blur-xl md:px-6">
      <div className="mx-auto flex max-w-[1600px] flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.28em] text-zinc-500">
            <span>{meta.eyebrow}</span>
            <span className="hidden text-zinc-700 sm:inline">/</span>
            <span>{now}</span>
          </div>
          <div className="mt-2 flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
            <div className="min-w-0">
              <h1 className="truncate text-2xl font-semibold text-white md:text-[2rem]">{title}</h1>
              <p className="mt-1 max-w-3xl text-sm text-zinc-400">{meta.subtitle}</p>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 lg:justify-end">
          <StatusBadge label="Mode" value={authMode === "api-key" ? "API key mode" : "Interactive"} tone="cyan" />
          <StatusBadge
            label="Operator"
            value={user?.name ?? (isAuthenticated ? "Authenticated" : "Guest")}
            tone="neutral"
          />
          {isAuthenticated ? (
            <button
              onClick={() => void logout()}
              disabled={authMode === "api-key"}
              className="rounded-2xl border border-rose-300/30 bg-rose-500/15 px-4 py-2 text-sm text-rose-100 disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
            >
              Logout
            </button>
          ) : (
            <button
              onClick={() => void login()}
              className="rounded-2xl border border-cyan-300/30 bg-cyan-500/15 px-4 py-2 text-sm text-cyan-50"
              type="button"
            >
              Login
            </button>
          )}
        </div>
      </div>
    </header>
  );
}

function StatusBadge({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "cyan" | "neutral";
}) {
  const toneClasses =
    tone === "cyan"
      ? "border-cyan-300/25 bg-cyan-400/10 text-cyan-50"
      : "border-white/10 bg-white/5 text-zinc-200";

  return (
    <div className={`rounded-2xl border px-3 py-2 ${toneClasses}`}>
      <p className="text-[10px] uppercase tracking-[0.24em] text-white/45">{label}</p>
      <p className="mt-1 text-sm font-medium">{value}</p>
    </div>
  );
}
