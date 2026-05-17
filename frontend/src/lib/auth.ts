"use client";

import { useCallback } from "react";
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

/**
 * Returns ``true`` when the app should treat the session as authenticated
 * even though no MSAL account is present. Three bypasses:
 *
 * - ``NODE_ENV === "development"`` — local dev loop (no Entra ID config required).
 * - ``NEXT_PUBLIC_E2E_AUTH_BYPASS === "1"`` — Playwright E2E mode (set by
 *   ``playwright.config.ts`` ``webServer.env``). Backend auth still gates
 *   API calls via ``X-API-Key``; this only bypasses the UI redirect.
 * - ``NEXT_PUBLIC_MSAI_API_KEY`` is set — dev/CI flow that uses API-key
 *   auth without MSAL.
 *
 * Each of these MUST be honored by every ``useQuery`` ``enabled`` flag in
 * the app (Codex iter-1 P1 — without this propagation, the new pages
 * stayed permanently pending in E2E because ``useAuth().isAuthenticated``
 * was ``false``).
 */
export function isAuthBypassed(): boolean {
  return (
    process.env.NODE_ENV === "development" ||
    process.env.NEXT_PUBLIC_E2E_AUTH_BYPASS === "1" ||
    Boolean(process.env.NEXT_PUBLIC_MSAI_API_KEY)
  );
}

export function useAuth(): AuthReturn {
  const { instance, accounts } = useMsal();
  const account = useAccount(accounts[0] || null);

  // Memoize every returned function so consumers can use them in
  // `useCallback` / `useEffect` dep arrays without triggering re-renders
  // or infinite loops. Identity is stable across renders until the
  // dependencies (`instance`, `account`) actually change.
  //
  // Codex code review 2026-04-21: the previous unmemoized definitions
  // produced a fresh closure on every render, which caused any caller's
  // `useCallback(..., [getToken])` → `useEffect(..., [load])` pair to
  // re-fire every render. Workarounds (eslint-disable + `[]` deps) would
  // have frozen the first-render MSAL `account` into the callback,
  // silently breaking auth after login completes. Memoizing at the
  // source fixes both failure modes at once.

  const login = useCallback(async (): Promise<void> => {
    await instance.loginRedirect(loginRequest);
  }, [instance]);

  const logout = useCallback(async (): Promise<void> => {
    await instance.logoutRedirect();
  }, [instance]);

  const getToken = useCallback(async (): Promise<string | null> => {
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
  }, [instance, account]);

  return {
    user: account ? { name: account.name, email: account.username } : null,
    // Honor the dev / E2E / API-key bypasses so downstream
    // ``useQuery({ enabled: isAuthenticated })`` hooks fire even when
    // MSAL has no account (Codex iter-1 P1).
    isAuthenticated: !!account || isAuthBypassed(),
    login,
    logout,
    getToken,
  };
}
