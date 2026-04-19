"use client";

import { useMsal, useAccount } from "@azure/msal-react";
import { loginRequest } from "./msal-config";

interface AuthUser {
  name: string | undefined;
  email: string;
}

interface AuthReturn {
  user: AuthUser | null;
  isAuthenticated: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
  getToken: () => Promise<string | null>;
}

export function useAuth(): AuthReturn {
  const { instance, accounts } = useMsal();
  const account = useAccount(accounts[0] || null);

  const login = async (): Promise<void> => {
    await instance.loginRedirect(loginRequest);
  };

  const logout = async (): Promise<void> => {
    await instance.logoutRedirect();
  };

  const getToken = async (): Promise<string | null> => {
    if (!account) return null;
    try {
      const response = await instance.acquireTokenSilent({
        ...loginRequest,
        account,
      });
      return response.accessToken;
    } catch {
      return null;
    }
  };

  return {
    user: account ? { name: account.name, email: account.username } : null,
    isAuthenticated: !!account,
    login,
    logout,
    getToken,
  };
}
