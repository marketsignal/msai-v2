"use client";

import { useEffect, useState } from "react";
import { StrategyCard } from "@/components/strategies/strategy-card";
import {
  apiGet,
  ApiError,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function StrategiesPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const data = await apiGet<StrategyListResponse>(
          "/api/v1/strategies/",
          token,
        );
        if (cancelled) return;
        setStrategies(data.items);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `Failed to load strategies (${err.status})`
            : "Failed to load strategies";
        setError(msg);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Strategies</h1>
        <p className="text-sm text-muted-foreground">
          Manage and monitor your trading strategies
        </p>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          Loading strategies...
        </div>
      ) : strategies.length === 0 ? (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          No strategies registered.
        </div>
      ) : (
        <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          {strategies.map((strategy) => (
            <StrategyCard
              key={strategy.id}
              strategy={{
                id: strategy.id,
                name: strategy.name,
                description: strategy.description,
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
