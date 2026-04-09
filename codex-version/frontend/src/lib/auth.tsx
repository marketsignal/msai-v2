"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { AccountInfo } from "@azure/msal-browser";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";

import { getApiKeyCredential, getAuthMode, type AuthMode, isApiKeyAuthMode } from "@/lib/auth-mode";
import { loginRequest } from "@/lib/msal-config";

type AuthUser = {
  name: string | null;
  username?: string | null;
};

type AuthContextValue = {
  user: AuthUser | null;
  token: string | null;
  authMode: AuthMode;
  isAuthenticated: boolean;
  loading: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (isApiKeyAuthMode()) {
    return <ApiKeyAuthProvider>{children}</ApiKeyAuthProvider>;
  }
  return <MsalAuthProvider>{children}</MsalAuthProvider>;
}

function ApiKeyAuthProvider({ children }: { children: React.ReactNode }) {
  const token = getApiKeyCredential();
  const value = useMemo<AuthContextValue>(
    () => ({
      user: {
        name: "E2E API Key",
        username: "api-key@msai.local",
      },
      token,
      authMode: "api-key",
      isAuthenticated: Boolean(token),
      loading: false,
      login: async () => {},
      logout: async () => {},
    }),
    [token],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function MsalAuthProvider({ children }: { children: React.ReactNode }) {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const account = accounts[0] ?? null;
  const user = account ? _mapAccount(account) : null;

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
      authMode: getAuthMode(),
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

function _mapAccount(account: AccountInfo): AuthUser {
  return {
    name: account.name ?? account.username ?? "MSAI Operator",
    username: account.username,
  };
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return ctx;
}
