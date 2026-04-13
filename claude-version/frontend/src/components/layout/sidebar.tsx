"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import {
  LayoutDashboard,
  TrendingUp,
  FlaskConical,
  Microscope,
  GraduationCap,
  PieChart,
  Radio,
  BarChart3,
  Database,
  Settings,
  Menu,
  X,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";

interface NavItem {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
}

const navItems: NavItem[] = [
  { label: "Dashboard", href: "/dashboard", icon: LayoutDashboard },
  { label: "Strategies", href: "/strategies", icon: TrendingUp },
  { label: "Backtests", href: "/backtests", icon: FlaskConical },
  { label: "Research", href: "/research", icon: Microscope },
  { label: "Graduation", href: "/graduation", icon: GraduationCap },
  { label: "Portfolio", href: "/portfolio", icon: PieChart },
  { label: "Live Trading", href: "/live-trading", icon: Radio },
  { label: "Market Data", href: "/market-data", icon: BarChart3 },
  { label: "Data Management", href: "/data-management", icon: Database },
  { label: "Settings", href: "/settings", icon: Settings },
];

function NavLink({
  item,
  isActive,
}: {
  item: NavItem;
  isActive: boolean;
}): React.ReactElement {
  const Icon = item.icon;
  return (
    <Button
      asChild
      variant="ghost"
      className={cn(
        "w-full justify-start gap-3 px-3 py-2 text-sm font-medium transition-colors",
        isActive
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Link href={item.href}>
        <Icon className="size-4 shrink-0" />
        {item.label}
      </Link>
    </Button>
  );
}

function SidebarContent(): React.ReactElement {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-14 items-center px-4">
        <Link href="/dashboard" className="flex items-center gap-2">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary">
            <span className="text-xs font-bold text-primary-foreground">M</span>
          </div>
          <span className="text-lg font-semibold tracking-tight">MSAI</span>
        </Link>
      </div>

      <Separator className="opacity-50" />

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-2 py-3">
        {navItems.map((item) => (
          <NavLink
            key={item.href}
            item={item}
            isActive={
              pathname === item.href || pathname.startsWith(`${item.href}/`)
            }
          />
        ))}
      </nav>

      {/* Footer */}
      <div className="px-4 py-3">
        <Separator className="mb-3 opacity-50" />
        <p className="text-xs text-muted-foreground">MarketSignal AI v2</p>
      </div>
    </div>
  );
}

export function Sidebar(): React.ReactElement {
  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden w-64 shrink-0 border-r border-border/50 bg-card md:block">
        <SidebarContent />
      </aside>
    </>
  );
}

export function MobileSidebarTrigger(): React.ReactElement {
  const [open, setOpen] = useState(false);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" className="md:hidden">
          {open ? <X className="size-5" /> : <Menu className="size-5" />}
          <span className="sr-only">Toggle navigation</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="w-64 p-0">
        <div onClick={() => setOpen(false)}>
          <SidebarContent />
        </div>
      </SheetContent>
    </Sheet>
  );
}
