import { StrategyCard } from "@/components/strategies/strategy-card";
import { strategies } from "@/lib/mock-data/strategies";

export default function StrategiesPage(): React.ReactElement {
  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Strategies</h1>
        <p className="text-sm text-muted-foreground">
          Manage and monitor your trading strategies
        </p>
      </div>

      {/* Strategy grid */}
      <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
        {strategies.map((strategy) => (
          <StrategyCard key={strategy.id} strategy={strategy} />
        ))}
      </div>
    </div>
  );
}
