"use client";

import { useEffect, useState } from "react";
import { MsalProvider } from "@azure/msal-react";
import {
  PublicClientApplication,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { msalConfig } from "@/lib/msal-config";

const msalInstance = new PublicClientApplication(msalConfig);

export function AuthProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement | null {
  const [instance, setInstance] = useState<IPublicClientApplication | null>(
    null,
  );

  useEffect(() => {
    msalInstance.initialize().then(() => {
      setInstance(msalInstance);
    });
  }, []);

  if (!instance) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="size-8 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
          <p className="text-sm text-muted-foreground">Loading...</p>
        </div>
      </div>
    );
  }

  return <MsalProvider instance={instance}>{children}</MsalProvider>;
}
