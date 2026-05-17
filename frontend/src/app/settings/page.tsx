"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { User, Shield, AlertTriangle } from "lucide-react";
import { useUserProfile } from "@/lib/hooks/use-user-profile";
import { describeApiError } from "@/lib/api";

/**
 * Settings — Trust-First profile page.
 *
 * Per the audit (docs/audits/2026-05-16-ui-surface-audit.md F-1..F-5)
 * and Revision R12, this page used to ship 8 fake elements: hardcoded
 * "Admin" role, fake notification toggles, fake save button, 6
 * hardcoded System Information rows, and a "Clear All Data" Danger
 * Zone button calling a nonexistent endpoint. All removed.
 *
 * What remains:
 *
 *   - Profile card driven by GET /api/v1/auth/me via useUserProfile().
 *     Shows real `display_name`, `email`, `role`. No fallback strings
 *     like "Demo User" — when the fetch is in flight we render a
 *     skeleton; if it errors we render an inline error.
 *
 * System Information moved to /system (T12). Notification preferences
 * dropped entirely until a backend persists them (R12). Danger Zone
 * cut — there is no /api/v1/admin/clear-data endpoint.
 */
export default function SettingsPage(): React.ReactElement {
  const profileQuery = useUserProfile();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Your account profile, sourced from Azure Entra ID.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="border-border/50">
          <CardHeader>
            <div className="flex items-center gap-2">
              <User
                className="size-4 text-muted-foreground"
                aria-hidden="true"
              />
              <CardTitle className="text-base">User Profile</CardTitle>
            </div>
            <CardDescription>
              Read-only — values come from your Entra ID claims.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {profileQuery.isPending ? (
              <ProfileSkeleton />
            ) : profileQuery.isError ? (
              <ProfileError
                message={describeApiError(
                  profileQuery.error,
                  "Failed to load profile",
                )}
              />
            ) : (
              <ProfileFields
                displayName={profileQuery.data.display_name}
                email={profileQuery.data.email}
                role={profileQuery.data.role}
              />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function ProfileFields({
  displayName,
  email,
  role,
}: {
  displayName: string | null;
  email: string;
  role: string | null;
}): React.ReactElement {
  return (
    <>
      <div className="space-y-2">
        <Label htmlFor="profile-display-name" className="text-muted-foreground">
          Display name
        </Label>
        <Input
          id="profile-display-name"
          value={displayName ?? email}
          readOnly
          className="bg-muted/50"
          data-testid="profile-display-name"
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="profile-email" className="text-muted-foreground">
          Email
        </Label>
        <Input
          id="profile-email"
          value={email}
          readOnly
          className="bg-muted/50"
          data-testid="profile-email"
        />
      </div>
      <div className="space-y-2">
        <Label className="text-muted-foreground">Role</Label>
        <div>
          <Badge
            variant="secondary"
            className="bg-muted text-muted-foreground"
            data-testid="profile-role"
          >
            <Shield className="mr-1 size-3" aria-hidden="true" />
            {role ?? "unassigned"}
          </Badge>
        </div>
      </div>
    </>
  );
}

function ProfileSkeleton(): React.ReactElement {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="space-y-2">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-10 w-full" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-4 w-16" />
        <Skeleton className="h-10 w-full" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-4 w-12" />
        <Skeleton className="h-6 w-24" />
      </div>
    </div>
  );
}

function ProfileError({ message }: { message: string }): React.ReactElement {
  return (
    <div
      className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3"
      role="alert"
    >
      <AlertTriangle
        className="mt-0.5 size-4 shrink-0 text-red-400"
        aria-hidden="true"
      />
      <div className="text-sm text-red-400">
        Failed to load profile: <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}
