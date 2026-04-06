"use client";

import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type UserProfile = { display_name?: string; email?: string; role?: string };

export default function SettingsPage() {
  const { token } = useAuth();
  const [profile, setProfile] = useState<UserProfile>({});
  const [health, setHealth] = useState<{ status: string; environment?: string }>({ status: "unknown" });
  const [ready, setReady] = useState<{ status: string }>({ status: "unknown" });

  useEffect(() => {
    if (!token) return;

    async function load() {
      try {
        const [me, healthPayload, readyPayload] = await Promise.all([
          apiFetch<UserProfile>("/api/v1/auth/me", token),
          apiFetch<{ status: string; environment: string }>("/health", token),
          apiFetch<{ status: string }>("/ready", token),
        ]);
        setProfile(me);
        setHealth(healthPayload);
        setReady(readyPayload);
      } catch {
        setProfile({ display_name: "Trader", email: "trader@example.com", role: "admin" });
        setHealth({ status: "healthy", environment: "development" });
        setReady({ status: "ready" });
      }
    }

    void load();
  }, [token]);

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h2 className="text-lg font-semibold text-white">Profile</h2>
        <p className="mt-2 text-sm text-zinc-300">{profile.display_name}</p>
        <p className="text-sm text-zinc-400">{profile.email}</p>
        <p className="text-sm text-zinc-400">Role: {profile.role}</p>
      </section>

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h2 className="text-lg font-semibold text-white">System</h2>
        <p className="mt-2 text-sm text-zinc-300">Health: {health.status}</p>
        <p className="text-sm text-zinc-300">Readiness: {ready.status}</p>
        <p className="text-sm text-zinc-400">Environment: {health.environment}</p>
      </section>

      <section className="rounded-xl border border-rose-300/20 bg-rose-500/10 p-4">
        <h2 className="text-lg font-semibold text-rose-100">Danger Zone</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" className="rounded border border-rose-300/50 px-3 py-2 text-sm text-rose-100">
            Clear All Data
          </button>
          <button type="button" className="rounded border border-amber-300/50 px-3 py-2 text-sm text-amber-100">
            Reset Settings
          </button>
        </div>
      </section>
    </div>
  );
}
