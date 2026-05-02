"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useDebounceValue } from "usehooks-ts";

import { getInventory, type AssetClass, type InventoryRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export type WindowChoice = "1y" | "2y" | "5y" | "10y" | "custom";

export function windowToDateRange(
  choice: WindowChoice,
  custom?: { start: string; end: string },
): { start: string; end: string } {
  if (choice === "custom" && custom) return custom;
  const today = new Date();
  const end = today.toISOString().slice(0, 10);
  const years =
    choice === "1y" ? 1 : choice === "2y" ? 2 : choice === "10y" ? 10 : 5;
  const start = new Date(
    today.getFullYear() - years,
    today.getMonth(),
    today.getDate(),
  )
    .toISOString()
    .slice(0, 10);
  return { start, end };
}

export interface UseInventoryQueryParams {
  windowChoice: WindowChoice;
  customRange?: { start: string; end: string };
  assetClass?: AssetClass;
}

export interface UseInventoryQueryReturn {
  data: InventoryRow[] | undefined;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
}

export function useInventoryQuery(
  params: UseInventoryQueryParams,
): UseInventoryQueryReturn {
  const { getToken } = useAuth();

  // Override O-5: debounce flat strings, not object identity.
  // `params.customRange` is a fresh object every render — debouncing it directly
  // would re-fire on every parent render and risk infinite loops.
  const customKey = params.customRange
    ? `${params.customRange.start}|${params.customRange.end}`
    : "";
  const [debouncedChoice] = useDebounceValue(params.windowChoice, 300);
  const [debouncedCustomKey] = useDebounceValue(customKey, 300);

  const range = useMemo(() => {
    if (debouncedChoice === "custom" && debouncedCustomKey) {
      const [start, end] = debouncedCustomKey.split("|");
      return windowToDateRange(debouncedChoice, { start, end });
    }
    return windowToDateRange(debouncedChoice);
  }, [debouncedChoice, debouncedCustomKey]);

  const query = useQuery({
    queryKey: ["inventory", range.start, range.end, params.assetClass ?? "all"],
    queryFn: async () => {
      const token = await getToken();
      // apiGet does not accept AbortSignal; queryKey-change dedup is sufficient
      // for the window-picker case in v1 (TanStack latest-key-wins).
      return getInventory(token, {
        start: range.start,
        end: range.end,
        asset_class: params.assetClass,
      });
    },
  });

  return {
    data: query.data,
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
  };
}
