"use client";

import { LogOut } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { MobileSidebarTrigger } from "./sidebar";

function getInitials(name: string | undefined): string {
  if (!name) return "U";
  return name
    .split(" ")
    .map((part) => part[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

export function Header(): React.ReactElement {
  const { user, logout } = useAuth();

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-border/50 bg-card px-4">
      <div className="flex items-center gap-2">
        <MobileSidebarTrigger />
      </div>

      <div className="flex items-center gap-3">
        {user && (
          <>
            <div className="hidden items-center gap-2 sm:flex">
              <Avatar className="size-7">
                <AvatarFallback className="bg-muted text-xs font-medium">
                  {getInitials(user.name)}
                </AvatarFallback>
              </Avatar>
              <div className="flex flex-col">
                <span className="text-sm font-medium leading-none">
                  {user.name}
                </span>
                <span className="text-xs text-muted-foreground">
                  {user.email}
                </span>
              </div>
            </div>

            {/* Mobile avatar */}
            <Avatar className="size-7 sm:hidden">
              <AvatarFallback className="bg-muted text-xs font-medium">
                {getInitials(user.name)}
              </AvatarFallback>
            </Avatar>
          </>
        )}

        <Button
          variant="ghost"
          size="sm"
          onClick={() => void logout()}
          className="gap-2 text-muted-foreground hover:text-foreground"
        >
          <LogOut className="size-4" />
          <span className="hidden sm:inline">Sign out</span>
        </Button>
      </div>
    </header>
  );
}
