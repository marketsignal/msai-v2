"use client";

/**
 * useSystemHealth — TanStack Query hook for GET /api/v1/system/health.
 *
 * Polls every 30 s per the research-validated cheat-sheet. The endpoint
 * returns subsystem statuses + version + commit SHA + uptime. The
 * IB Gateway sub-probe reads cached state (`_ib_probe`), but the DB and
 * Redis sub-probes still do fresh per-request `SELECT 1` / `PING` with
 * tight timeouts (~500 ms each). 30 s polling against the DB ping is
 * acceptable today but becomes a budget concern at scale — see Codex
 * iter-1 P1 in the plan; a follow-up will introduce a SystemHealthCache
 * analog to IBAccountSnapshot if poll budgets tighten.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { getSystemHealth, type SystemHealthResponse } from "@/lib/api";

export const SYSTEM_HEALTH_KEY = ["system", "health"] as const;

export function useSystemHealth(): UseQueryResult<SystemHealthResponse, Error> {
  const { getToken, isAuthenticated } = useAuth();
  return useQuery<SystemHealthResponse, Error>({
    queryKey: SYSTEM_HEALTH_KEY,
    queryFn: async (): Promise<SystemHealthResponse> => {
      const token = await getToken();
      return getSystemHealth(token);
    },
    enabled: isAuthenticated,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: 1,
  });
}
