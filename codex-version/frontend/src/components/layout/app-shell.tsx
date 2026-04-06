"use client";

import { usePathname } from "next/navigation";

import { Header } from "@/components/layout/header";
import { Sidebar } from "@/components/layout/sidebar";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (pathname === "/login") {
    return <>{children}</>;
  }

  return (
    <div className="min-h-screen md:grid md:grid-cols-[16rem_1fr]">
      <Sidebar />
      <div>
        <Header />
        <main className="px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}
