"use client";

import { useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { User, Bell, Server, Trash2, Shield } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

export default function SettingsPage(): React.ReactElement {
  const { user, getToken } = useAuth();
  const [alertEmail, setAlertEmail] = useState(user?.email ?? "");
  const [clearDialogOpen, setClearDialogOpen] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const handleClearAllData = async (): Promise<void> => {
    setClearError(null);
    try {
      const token = await getToken();
      const response = await apiFetch(
        "/api/v1/admin/clear-data",
        { method: "DELETE" },
        token,
      );
      if (!response.ok) {
        setClearError(
          "Failed to clear data. This feature may not be available yet.",
        );
      }
    } catch (error) {
      console.error("Clear all data failed:", error);
      setClearError("Failed to clear data. The backend may not be running.");
    }
    setClearDialogOpen(false);
  };

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Manage your account, preferences, and system configuration
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* User profile */}
        <Card className="border-border/50">
          <CardHeader>
            <div className="flex items-center gap-2">
              <User className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">User Profile</CardTitle>
            </div>
            <CardDescription>
              Your account information from Azure AD
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label className="text-muted-foreground">Name</Label>
              <Input
                value={user?.name ?? "Demo User"}
                readOnly
                className="bg-muted/50"
              />
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Email</Label>
              <Input
                value={user?.email ?? "demo@msai.dev"}
                readOnly
                className="bg-muted/50"
              />
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Role</Label>
              <div>
                <Badge
                  variant="secondary"
                  className="bg-blue-500/15 text-blue-500"
                >
                  <Shield className="mr-1 size-3" />
                  Admin
                </Badge>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Notification preferences */}
        <Card className="border-border/50">
          <CardHeader>
            <div className="flex items-center gap-2">
              <Bell className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">Notifications</CardTitle>
            </div>
            <CardDescription>
              Configure how you receive trading alerts
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>Alert Email</Label>
              <Input
                type="email"
                value={alertEmail}
                onChange={(e) => setAlertEmail(e.target.value)}
                placeholder="your@email.com"
              />
              <p className="text-xs text-muted-foreground">
                Receive trading alerts, error notifications, and daily summaries
              </p>
            </div>
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div>
                  <p className="text-sm font-medium">Trade Execution Alerts</p>
                  <p className="text-xs text-muted-foreground">
                    Notify on each trade execution
                  </p>
                </div>
                <Badge
                  variant="secondary"
                  className="bg-emerald-500/15 text-emerald-500"
                >
                  On
                </Badge>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div>
                  <p className="text-sm font-medium">Strategy Error Alerts</p>
                  <p className="text-xs text-muted-foreground">
                    Notify when a strategy encounters an error
                  </p>
                </div>
                <Badge
                  variant="secondary"
                  className="bg-emerald-500/15 text-emerald-500"
                >
                  On
                </Badge>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div>
                  <p className="text-sm font-medium">Daily Summary</p>
                  <p className="text-xs text-muted-foreground">
                    End-of-day P&L report via email
                  </p>
                </div>
                <Badge
                  variant="secondary"
                  className="bg-muted text-muted-foreground"
                >
                  Off
                </Badge>
              </div>
            </div>
            <Button size="sm">Save Preferences</Button>
          </CardContent>
        </Card>

        {/* System info */}
        <Card className="border-border/50">
          <CardHeader>
            <div className="flex items-center gap-2">
              <Server className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">System Information</CardTitle>
            </div>
            <CardDescription>
              Current system status and configuration
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Version</span>
                <span className="font-mono text-sm">v0.1.0</span>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Environment</span>
                <Badge variant="outline" className="text-xs font-normal">
                  development
                </Badge>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Uptime</span>
                <span className="font-mono text-sm text-muted-foreground">
                  5d 14h 32m
                </span>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Disk Usage</span>
                <span className="font-mono text-sm text-muted-foreground">
                  17.75 GB / 100 GB
                </span>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">API Status</span>
                <div className="flex items-center gap-1.5">
                  <div className="size-2 rounded-full bg-emerald-500" />
                  <span className="text-sm text-emerald-500">Healthy</span>
                </div>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Database</span>
                <div className="flex items-center gap-1.5">
                  <div className="size-2 rounded-full bg-emerald-500" />
                  <span className="text-sm text-emerald-500">Connected</span>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Danger zone */}
        <Card className="border-red-500/20">
          <CardHeader>
            <div className="flex items-center gap-2">
              <Trash2 className="size-4 text-red-400" />
              <CardTitle className="text-base text-red-400">
                Danger Zone
              </CardTitle>
            </div>
            <CardDescription>
              Destructive actions that cannot be undone
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Separator className="opacity-50" />
            {clearError && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3">
                <p className="text-sm text-red-400">{clearError}</p>
              </div>
            )}
            <div className="space-y-3">
              <div className="rounded-lg border border-red-500/20 p-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium">Clear All Data</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Permanently delete all market data, backtest results,
                      trade history, and strategy configurations. This action is
                      irreversible.
                    </p>
                  </div>
                  <Dialog
                    open={clearDialogOpen}
                    onOpenChange={setClearDialogOpen}
                  >
                    <DialogTrigger asChild>
                      <Button variant="destructive" size="sm">
                        Clear All Data
                      </Button>
                    </DialogTrigger>
                    <DialogContent>
                      <DialogHeader>
                        <DialogTitle>Clear All Data</DialogTitle>
                        <DialogDescription>
                          This will permanently delete all market data, backtest
                          results, trade history, and strategy configurations.
                          This action cannot be undone.
                        </DialogDescription>
                      </DialogHeader>
                      <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
                        <p className="text-sm text-red-400">
                          Type &quot;DELETE&quot; to confirm this action.
                        </p>
                      </div>
                      <DialogFooter className="gap-2">
                        <Button
                          variant="outline"
                          onClick={() => setClearDialogOpen(false)}
                        >
                          Cancel
                        </Button>
                        <Button
                          variant="destructive"
                          onClick={handleClearAllData}
                        >
                          Clear All Data
                        </Button>
                      </DialogFooter>
                    </DialogContent>
                  </Dialog>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
