import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FlaskConical, TrendingUp, Trophy, BarChart3 } from "lucide-react";
import { formatPercent } from "@/lib/format";

export interface StrategyCardProps {
  strategy: {
    id: string;
    name: string;
    description: string;
    status?: "running" | "stopped" | "error";
    sharpeRatio?: number;
    totalReturn?: number;
    winRate?: number;
    instruments?: string[];
  };
}

function statusColor(status: "running" | "stopped" | "error"): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "bg-muted text-muted-foreground hover:bg-muted";
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
  }
}

export function StrategyCard({
  strategy,
}: StrategyCardProps): React.ReactElement {
  const status = strategy.status ?? "stopped";
  const sharpeRatio = strategy.sharpeRatio ?? 0;
  const totalReturn = strategy.totalReturn ?? 0;
  const winRate = strategy.winRate ?? 0;
  const instruments = strategy.instruments ?? [];
  const hasMetrics =
    strategy.sharpeRatio !== undefined ||
    strategy.totalReturn !== undefined ||
    strategy.winRate !== undefined;

  return (
    <Card className="border-border/50 flex flex-col">
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">{strategy.name}</CardTitle>
            <CardDescription className="line-clamp-2">
              {strategy.description}
            </CardDescription>
          </div>
          <Badge variant="secondary" className={statusColor(status)}>
            {status}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="flex-1">
        <div className="grid grid-cols-3 gap-4">
          <div className="space-y-1">
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <BarChart3 className="size-3" />
              Sharpe
            </div>
            <p className="text-lg font-semibold">
              {hasMetrics ? sharpeRatio.toFixed(2) : "--"}
            </p>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <TrendingUp className="size-3" />
              Return
            </div>
            <p
              className={`text-lg font-semibold ${
                hasMetrics
                  ? totalReturn >= 0
                    ? "text-emerald-500"
                    : "text-red-500"
                  : ""
              }`}
            >
              {hasMetrics ? formatPercent(totalReturn) : "--"}
            </p>
          </div>
          <div className="space-y-1">
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <Trophy className="size-3" />
              Win Rate
            </div>
            <p className="text-lg font-semibold">
              {hasMetrics ? `${winRate.toFixed(1)}%` : "--"}
            </p>
          </div>
        </div>

        <div className="mt-4">
          <p className="text-xs text-muted-foreground">Instruments</p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {instruments.length === 0 ? (
              <span className="text-xs text-muted-foreground">--</span>
            ) : (
              instruments.map((inst) => (
                <Badge
                  key={inst}
                  variant="outline"
                  className="text-xs font-normal"
                >
                  {inst}
                </Badge>
              ))
            )}
          </div>
        </div>
      </CardContent>

      <CardFooter className="gap-2 border-t border-border/50 pt-4">
        <Button asChild variant="outline" size="sm" className="flex-1">
          <Link href={`/strategies/${strategy.id}`}>View Details</Link>
        </Button>
        <Button asChild size="sm" className="flex-1 gap-1.5">
          <Link href={`/backtests?strategy=${strategy.id}`}>
            <FlaskConical className="size-3.5" />
            Run Backtest
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}
