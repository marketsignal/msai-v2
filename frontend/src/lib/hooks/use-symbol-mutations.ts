"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  deleteSymbol,
  postOnboard,
  type AssetClass,
  type OnboardRequest,
  type OnboardResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface RefreshArgs {
  symbol: string;
  asset_class: AssetClass;
  start: string;
  end: string;
}

/**
 * Convert a symbol to a slug safe for `OnboardRequest.watchlist_name`, which
 * the backend validates against `^[a-z0-9-]+$`. Symbols may contain `.`, `_`,
 * `/` (e.g. `BRK.B`, `ES.c.0`, `EUR/USD`); collapse those to hyphens.
 */
export function slugifySymbol(symbol: string): string {
  return symbol.toLowerCase().replace(/[^a-z0-9-]+/g, "-");
}

interface RemoveArgs {
  symbol: string;
  asset_class: AssetClass;
}

export interface UseRefreshSymbolReturn {
  mutate: (args: RefreshArgs) => void;
  isPending: boolean;
}

export interface UseRefreshSymbolOptions {
  /**
   * Called with the new `run_id` when the onboard request returns 202. Pages
   * use this to register the run as "active" so a background poller can
   * invalidate the inventory query on terminal status — without this, the
   * UI never refetches after the worker completes (mutation onSuccess fires
   * on POST 202, NOT on worker completion).
   */
  onRunStarted?: (runId: string) => void;
}

export function useRefreshSymbol(
  options: UseRefreshSymbolOptions = {},
): UseRefreshSymbolReturn {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: async (args: RefreshArgs): Promise<OnboardResponse> => {
      const token = await getToken();
      const body: OnboardRequest = {
        watchlist_name: `ui-refresh-${slugifySymbol(args.symbol)}-${Date.now()}`,
        symbols: [
          {
            symbol: args.symbol,
            asset_class: args.asset_class,
            start: args.start,
            end: args.end,
          },
        ],
      };
      return postOnboard(token, body);
    },
    onSuccess: (resp) => {
      toast.success(`Refresh queued (run ${resp.run_id.slice(0, 8)}…)`);
      qc.invalidateQueries({ queryKey: ["inventory"] });
      options.onRunStarted?.(resp.run_id);
    },
    onError: (err) => {
      toast.error(`Refresh failed: ${String(err)}`);
    },
  });
  return { mutate: m.mutate, isPending: m.isPending };
}

export interface UseRemoveSymbolReturn {
  mutate: (args: RemoveArgs) => void;
  isPending: boolean;
}

export function useRemoveSymbol(): UseRemoveSymbolReturn {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: async (args: RemoveArgs): Promise<void> => {
      const token = await getToken();
      return deleteSymbol(token, args);
    },
    onSuccess: () => {
      toast.success("Symbol removed from inventory");
      qc.invalidateQueries({ queryKey: ["inventory"] });
    },
    onError: (err) => {
      toast.error(`Remove failed: ${String(err)}`);
    },
  });
  return { mutate: m.mutate, isPending: m.isPending };
}
