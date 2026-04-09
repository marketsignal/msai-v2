"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_GROUPS = [
  {
    label: "Operate",
    items: [
      { href: "/dashboard", label: "Desk Overview", short: "OV" },
      { href: "/live", label: "Live Command", short: "LV" },
      { href: "/data", label: "Data Control", short: "DT" },
    ],
  },
  {
    label: "Workflow",
    items: [
      { href: "/research", label: "Research Console", short: "RS" },
      { href: "/graduation", label: "Graduation", short: "GR" },
      { href: "/portfolio", label: "Portfolio Lab", short: "PF" },
    ],
  },
  {
    label: "Registry",
    items: [
      { href: "/backtests", label: "Backtests", short: "BT" },
      { href: "/strategies", label: "Strategies", short: "ST" },
      { href: "/market-data", label: "Market Data", short: "MD" },
    ],
  },
  {
    label: "System",
    items: [{ href: "/settings", label: "Settings", short: "SE" }],
  },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sticky top-0 hidden h-screen overflow-y-auto border-r border-white/10 bg-[linear-gradient(180deg,rgba(9,13,20,0.96),rgba(7,10,16,0.92))] p-4 backdrop-blur-xl md:block">
      <div className="rounded-[1.5rem] border border-cyan-300/20 bg-[radial-gradient(circle_at_top_left,rgba(45,212,191,0.22),transparent_55%),linear-gradient(180deg,rgba(22,31,45,0.9),rgba(10,14,21,0.9))] p-5 shadow-[0_18px_60px_rgba(0,0,0,0.35)]">
        <p className="text-[11px] uppercase tracking-[0.32em] text-cyan-200/80">MSAI v2</p>
        <p className="mt-3 text-2xl font-semibold text-white">Alpha Desk</p>
        <p className="mt-2 text-sm leading-6 text-zinc-300">
          Research, portfolio construction, and live execution on one control surface.
        </p>
        <div className="mt-5 grid gap-2 sm:grid-cols-2">
          <SidebarPulse label="API-first" value="Ready" tone="cyan" />
          <SidebarPulse label="Realtime" value="Redis bus" tone="violet" />
        </div>
      </div>

      <div className="mt-6 space-y-6">
        {NAV_GROUPS.map((group) => (
          <section key={group.label}>
            <p className="px-2 text-[11px] uppercase tracking-[0.28em] text-zinc-500">{group.label}</p>
            <nav className="mt-3 space-y-1.5">
              {group.items.map((item) => {
                const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`group flex items-center gap-3 rounded-2xl border px-3 py-3 text-sm transition ${
                      active
                        ? "border-cyan-300/30 bg-cyan-400/15 text-white shadow-[0_12px_40px_rgba(34,211,238,0.12)]"
                        : "border-transparent text-zinc-300 hover:border-white/10 hover:bg-white/5 hover:text-white"
                    }`}
                  >
                    <span
                      className={`flex h-9 w-9 items-center justify-center rounded-xl border text-[11px] font-semibold tracking-[0.18em] ${
                        active
                          ? "border-cyan-300/40 bg-cyan-300/10 text-cyan-100"
                          : "border-white/10 bg-white/5 text-zinc-400 group-hover:text-zinc-200"
                      }`}
                    >
                      {item.short}
                    </span>
                    <div className="min-w-0">
                      <p className="truncate font-medium">{item.label}</p>
                      <p className="truncate text-xs text-zinc-500">
                        {active ? "Current workspace" : "Open module"}
                      </p>
                    </div>
                  </Link>
                );
              })}
            </nav>
          </section>
        ))}
      </div>
    </aside>
  );
}

function SidebarPulse({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "cyan" | "violet";
}) {
  const classes =
    tone === "cyan"
      ? "border-cyan-300/20 bg-cyan-400/10 text-cyan-50"
      : "border-violet-300/20 bg-violet-400/10 text-violet-50";

  return (
    <div className={`rounded-2xl border px-3 py-3 ${classes}`}>
      <p className="text-[10px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-1 text-sm font-semibold">{value}</p>
    </div>
  );
}
