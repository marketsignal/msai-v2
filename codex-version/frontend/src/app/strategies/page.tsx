"use client";

import { useEffect, useState } from "react";

import { StrategyCard } from "@/components/strategies/strategy-card";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type Strategy = {
  id: string;
  name: string;
  description?: string | null;
  strategy_class: string;
};

export default function StrategiesPage() {
  const { token } = useAuth();
  const [items, setItems] = useState<Strategy[]>([]);

  useEffect(() => {
    if (!token) return;
    async function load() {
      try {
        const strategies = await apiFetch<Strategy[]>("/api/v1/strategies/", token);
        setItems(strategies);
      } catch {
        setItems([
          { id: "demo-ema", name: "example.ema_cross", description: "EMA crossover baseline", strategy_class: "EMACrossStrategy" },
        ]);
      }
    }
    void load();
  }, [token]);

  return (
    <div className="space-y-4">
      <h2 className="text-2xl font-semibold text-white">Strategy Registry</h2>
      <div className="grid gap-4 lg:grid-cols-2">
        {items.map((item) => (
          <StrategyCard
            key={item.id}
            id={item.id}
            name={item.name}
            description={item.description}
            status="ready"
            sharpe={1.42}
          />
        ))}
      </div>
    </div>
  );
}
