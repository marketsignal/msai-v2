"use client";

import { useEffect, useState } from "react";
import { PublicClientApplication } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";

import { AuthProvider } from "@/lib/auth";
import { msalConfig } from "@/lib/msal-config";

export function AppProviders({ children }: { children: React.ReactNode }) {
  const [instance] = useState(() => new PublicClientApplication(msalConfig));
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let mounted = true;
    void instance.initialize().then(() => {
      if (mounted) {
        setReady(true);
      }
    });
    return () => {
      mounted = false;
    };
  }, [instance]);

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-zinc-300">
        Booting secure console...
      </div>
    );
  }

  return (
    <MsalProvider instance={instance}>
      <AuthProvider>{children}</AuthProvider>
    </MsalProvider>
  );
}
