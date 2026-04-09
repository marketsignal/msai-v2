"use client";

import { getAuthMode } from "@/lib/auth-mode";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { login, loading } = useAuth();
  const authMode = getAuthMode();

  if (authMode === "api-key") {
    return (
      <div className="relative flex min-h-screen items-center justify-center px-6">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(34,211,238,0.16),_transparent_40%),radial-gradient(circle_at_bottom,_rgba(245,158,11,0.16),_transparent_35%)]" />
        <div className="relative w-full max-w-md rounded-2xl border border-white/10 bg-black/40 p-8 backdrop-blur">
          <p className="text-xs uppercase tracking-[0.2em] text-cyan-300">MSAI v2</p>
          <h1 className="mt-3 text-3xl font-semibold text-white">API Key Test Mode</h1>
          <p className="mt-2 text-sm text-zinc-300">
            Browser auth is bypassed for E2E runs. The frontend will use the configured API key
            against the backend control plane.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-6">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(34,211,238,0.16),_transparent_40%),radial-gradient(circle_at_bottom,_rgba(245,158,11,0.16),_transparent_35%)]" />
      <div className="relative w-full max-w-md rounded-2xl border border-white/10 bg-black/40 p-8 backdrop-blur">
        <p className="text-xs uppercase tracking-[0.2em] text-cyan-300">MSAI v2</p>
        <h1 className="mt-3 text-3xl font-semibold text-white">Trade Control Login</h1>
        <p className="mt-2 text-sm text-zinc-300">
          Authenticate with Azure Entra ID to access backtests, live deployments, and market data controls.
        </p>
        <button
          type="button"
          onClick={() => void login()}
          disabled={loading}
          className="mt-6 w-full rounded-lg border border-cyan-300/40 bg-cyan-500/20 px-4 py-3 text-sm font-medium text-cyan-100 transition hover:bg-cyan-500/30 disabled:opacity-60"
        >
          {loading ? "Signing in..." : "Sign in with Entra ID"}
        </button>
      </div>
    </div>
  );
}
