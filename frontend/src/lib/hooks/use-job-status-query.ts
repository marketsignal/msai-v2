"use client";

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getOnboardStatus, type OnboardStatusResponse } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { computeRefetchInterval } from "./refetch-policy";

export interface UseJobStatusQueryReturn {
  data: OnboardStatusResponse | undefined;
  isLoading: boolean;
}

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "completed_with_failures",
]);

export function useJobStatusQuery(
  runId: string | null,
): UseJobStatusQueryReturn {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  // Refs persist across renders without triggering them; mutated inside
  // refetchInterval which TanStack invokes outside the render phase, so this
  // is safe under React 19 strict mode (Override O-16).
  const prevStatusRef = useRef<string | undefined>(undefined);
  const sameCountRef = useRef(0);
  // Tracks whether we've already invalidated inventory for this terminal
  // transition so a stable terminal poll doesn't re-fire invalidation each
  // render.
  const invalidatedForTerminalRef = useRef(false);

  const query = useQuery({
    queryKey: ["job-status", runId],
    enabled: runId !== null,
    queryFn: async () => {
      const token = await getToken();
      // runId is non-null when enabled=true (TanStack guarantees enabled
      // gates queryFn). Assert here so callers see a typed string.
      if (runId === null) {
        throw new Error("useJobStatusQuery: runId is null while enabled");
      }
      return getOnboardStatus(token, runId);
    },
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      if (status === prevStatusRef.current) {
        sameCountRef.current += 1;
      } else {
        sameCountRef.current = 0;
      }
      const interval = computeRefetchInterval({
        status,
        prevStatus: prevStatusRef.current,
        consecutiveSameCount: sameCountRef.current,
      });
      prevStatusRef.current = status;
      return interval;
    },
    refetchIntervalInBackground: false,
  });

  // E2E rerun fix: when a job reaches terminal status, invalidate the
  // inventory query so any downstream UI (table, drawer Coverage section)
  // re-derives against the post-completion state. The mutation's onSuccess
  // fires on POST 202 (worker not done yet), so without this we'd never
  // refetch inventory after the worker actually finishes its run.
  useEffect(() => {
    const status = query.data?.status;
    if (!status) return;
    if (TERMINAL_STATUSES.has(status)) {
      if (!invalidatedForTerminalRef.current) {
        invalidatedForTerminalRef.current = true;
        qc.invalidateQueries({ queryKey: ["inventory"] });
      }
    } else {
      invalidatedForTerminalRef.current = false;
    }
  }, [query.data?.status, qc]);

  return { data: query.data, isLoading: query.isLoading };
}
