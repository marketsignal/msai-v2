"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { AccountInfo } from "@azure/msal-browser";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";

import { loginRequest } from "@/lib/msal-config";

type AuthContextValue = {
  user: AccountInfo | null;
  token: string | null;
  isAuthenticated: boolean;
  loading: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const user = accounts[0] ?? null;

  useEffect(() => {
    let mounted = true;
    async function refreshToken() {
      if (!isAuthenticated || !accounts[0]) {
        if (mounted) {
          setToken(null);
        }
        return;
      }

      try {
        setLoading(true);
        const result = await instance.acquireTokenSilent({
          ...loginRequest,
          account: accounts[0],
        });
        if (mounted) {
          setToken(result.accessToken);
        }
      } catch {
        if (mounted) {
          setToken(null);
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    void refreshToken();
    return () => {
      mounted = false;
    };
  }, [accounts, instance, isAuthenticated]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      token,
      isAuthenticated,
      loading,
      login: async () => {
        await instance.loginRedirect(loginRequest);
      },
      logout: async () => {
        await instance.logoutRedirect({ postLogoutRedirectUri: "/login" });
      },
    }),
    [instance, isAuthenticated, loading, token, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return ctx;
}
