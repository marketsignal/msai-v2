"use client";

/**
 * useAccount* — three TanStack Query hooks for the broker account
 * dashboard, all served from the IBAccountSnapshot singleton on the
 * backend (one long-lived IB connection, 30 s background refresh).
 *
 * Polling cadence per the research-validated cheat-sheet (R-iter): 30 s,
 * aligned with the snapshot's refresh loop.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import {
  getAccountSummary,
  getAccountPortfolio,
  getAccountHealth,
  type AccountSummary,
  type AccountPortfolioItem,
  type AccountHealth,
} from "@/lib/api";

const REFETCH_MS = 30_000;
const STALE_MS = 15_000;

export const ACCOUNT_SUMMARY_KEY = ["account", "summary"] as const;
export const ACCOUNT_PORTFOLIO_KEY = ["account", "portfolio"] as const;
export const ACCOUNT_HEALTH_KEY = ["account", "health"] as const;

export function useAccountSummary(): UseQueryResult<AccountSummary, Error> {
  const { getToken, isAuthenticated } = useAuth();
  return useQuery<AccountSummary, Error>({
    queryKey: ACCOUNT_SUMMARY_KEY,
    queryFn: async (): Promise<AccountSummary> => {
      const token = await getToken();
      return getAccountSummary(token);
    },
    enabled: isAuthenticated,
    refetchInterval: REFETCH_MS,
    staleTime: STALE_MS,
    retry: 1,
  });
}

export function useAccountPortfolio(): UseQueryResult<
  AccountPortfolioItem[],
  Error
> {
  const { getToken, isAuthenticated } = useAuth();
  return useQuery<AccountPortfolioItem[], Error>({
    queryKey: ACCOUNT_PORTFOLIO_KEY,
    queryFn: async (): Promise<AccountPortfolioItem[]> => {
      const token = await getToken();
      return getAccountPortfolio(token);
    },
    enabled: isAuthenticated,
    refetchInterval: REFETCH_MS,
    staleTime: STALE_MS,
    retry: 1,
  });
}

export function useAccountHealth(): UseQueryResult<AccountHealth, Error> {
  const { getToken, isAuthenticated } = useAuth();
  return useQuery<AccountHealth, Error>({
    queryKey: ACCOUNT_HEALTH_KEY,
    queryFn: async (): Promise<AccountHealth> => {
      const token = await getToken();
      return getAccountHealth(token);
    },
    enabled: isAuthenticated,
    refetchInterval: REFETCH_MS,
    staleTime: STALE_MS,
    retry: 1,
  });
}
