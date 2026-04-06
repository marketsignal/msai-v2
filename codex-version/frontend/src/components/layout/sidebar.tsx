"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/strategies", label: "Strategies" },
  { href: "/backtests", label: "Backtests" },
  { href: "/market-data", label: "Market Data" },
  { href: "/live", label: "Live Trading" },
  { href: "/data", label: "Data Mgmt" },
  { href: "/settings", label: "Settings" },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="hidden border-r border-white/10 bg-black/30 p-4 backdrop-blur md:block md:w-64">
      <div className="mb-8 rounded-xl border border-cyan-300/20 bg-cyan-500/10 p-4">
        <p className="text-xs uppercase tracking-[0.2em] text-cyan-200">MSAI v2</p>
        <p className="mt-2 text-xl font-semibold text-white">Alpha Desk</p>
      </div>
      <nav className="space-y-2">
        {NAV.map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded-lg px-3 py-2 text-sm transition ${
                active
                  ? "bg-cyan-400/20 text-cyan-100"
                  : "text-zinc-300 hover:bg-white/5 hover:text-white"
              }`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
