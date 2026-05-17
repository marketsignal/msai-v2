"use client";

/**
 * useAlerts — TanStack Query hook for the alerts list (R6/R22).
 *
 * Backend AlertRecord has minimal fields (type, level, title, message,
 * created_at) and there is no per-alert identifier or read/unread state
 * — so the UI keys rows by response-index and the header bell counts
 * "alerts in the last 24h" rather than "unread."
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { getAlerts, type AlertListResponse } from "@/lib/api";

export const ALERTS_QUERY_KEY = ["alerts"] as const;

export function useAlerts(
  limit: number = 200,
): UseQueryResult<AlertListResponse, Error> {
  const { getToken, isAuthenticated } = useAuth();
  return useQuery<AlertListResponse, Error>({
    queryKey: [...ALERTS_QUERY_KEY, limit],
    queryFn: async (): Promise<AlertListResponse> => {
      const token = await getToken();
      return getAlerts(token, limit);
    },
    enabled: isAuthenticated,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1,
  });
}
