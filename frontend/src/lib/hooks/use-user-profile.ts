"use client";

/**
 * useUserProfile — TanStack Query hook that fetches /api/v1/auth/me.
 *
 * Per Revision R12 (and research finding 7 in
 * docs/research/2026-05-16-ui-completeness.md), the backend is the source
 * of truth for `role` and `display_name`. MSAL `account.idTokenClaims`
 * has `name` + `email` only — it cannot tell the UI whether the user is
 * a `viewer` / `admin` / etc. The Settings page (and any role-gated UI)
 * MUST consume this hook instead of `useAuth()` for role-aware reads.
 *
 * `useAuth()` remains the MSAL-focused hook (login, logout, getToken).
 * They compose: this hook calls `useAuth().getToken()` then fetches.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { getUserProfile, type UserProfile } from "@/lib/api";

const USER_PROFILE_KEY = ["user", "profile"] as const;

export function useUserProfile(): UseQueryResult<UserProfile, Error> {
  const { getToken, isAuthenticated } = useAuth();

  return useQuery<UserProfile, Error>({
    queryKey: USER_PROFILE_KEY,
    queryFn: async (): Promise<UserProfile> => {
      const token = await getToken();
      return getUserProfile(token);
    },
    enabled: isAuthenticated,
    // Profile rarely changes mid-session; 5-min stale window matches
    // QueryProviders defaults but is explicit here for documentation.
    staleTime: 5 * 60_000,
    retry: 1,
  });
}
