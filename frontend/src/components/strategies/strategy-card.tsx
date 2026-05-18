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
import {
  FlaskConical,
  ExternalLink,
  CheckCircle2,
  AlertTriangle,
  Square,
  CircleDashed,
} from "lucide-react";

/**
 * StrategyCard — list-page card.
 *
 * Per audit F-12, the previous version showed permanent "--" placeholders
 * for Sharpe / Return / Win Rate metrics because the parent passed only
 * {id, name, description}. Those columns are GONE — the detail page is
 * the canonical place for backtest metrics. We surface real deployment
 * status (running / stopped / no-active) when joined with /live/status,
 * and a "Run backtest" CTA so the trader can act from here.
 */
export type StrategyDeploymentStatus = "running" | "stopped" | "error" | "none";

export interface StrategyCardProps {
  strategy: {
    id: string;
    name: string;
    description: string;
    /** Real status from /live/status join, or "none" if no active deployment. */
    deploymentStatus?: StrategyDeploymentStatus;
  };
}

export function StrategyCard({
  strategy,
}: StrategyCardProps): React.ReactElement {
  const status = strategy.deploymentStatus ?? "none";
  return (
    <Card
      className="flex flex-col border-border/50"
      data-testid="strategy-card"
      data-strategy-id={strategy.id}
    >
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1">
            <CardTitle className="text-base">{strategy.name}</CardTitle>
            <CardDescription className="line-clamp-2">
              {strategy.description}
            </CardDescription>
          </div>
          <StatusBadge status={status} />
        </div>
      </CardHeader>

      <CardContent className="flex-1">
        <div className="space-y-2 rounded-md border border-border/50 p-3 text-xs text-muted-foreground">
          <p>
            Backtest metrics and per-run trade logs live on the detail page.
          </p>
          <p>
            Add new strategies by dropping a Python file into{" "}
            <code className="font-mono">strategies/</code> and committing —
            registry sync picks it up automatically.
          </p>
        </div>
      </CardContent>

      <CardFooter className="gap-2 border-t border-border/50 pt-4">
        <Button asChild variant="outline" size="sm" className="flex-1 gap-2">
          <Link href={`/strategies/${strategy.id}`}>
            <ExternalLink className="size-3.5" aria-hidden="true" />
            View details
          </Link>
        </Button>
        <Button asChild size="sm" className="flex-1 gap-2">
          <Link href={`/backtests?strategy=${strategy.id}`}>
            <FlaskConical className="size-3.5" aria-hidden="true" />
            Run backtest
          </Link>
        </Button>
      </CardFooter>
    </Card>
  );
}

function StatusBadge({
  status,
}: {
  status: StrategyDeploymentStatus;
}): React.ReactElement {
  // Trust-First: color + icon + text (Code Review iter-1 P1 #3).
  if (status === "running") {
    return (
      <Badge
        variant="secondary"
        className="gap-1 bg-emerald-500/15 text-emerald-400"
      >
        <CheckCircle2 className="size-3" aria-hidden="true" />
        running
      </Badge>
    );
  }
  if (status === "error") {
    return (
      <Badge variant="secondary" className="gap-1 bg-red-500/15 text-red-400">
        <AlertTriangle className="size-3" aria-hidden="true" />
        error
      </Badge>
    );
  }
  if (status === "stopped") {
    return (
      <Badge
        variant="secondary"
        className="gap-1 bg-muted text-muted-foreground"
      >
        <Square className="size-3" aria-hidden="true" />
        stopped
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="gap-1 text-muted-foreground">
      <CircleDashed className="size-3" aria-hidden="true" />
      no deployment
    </Badge>
  );
}
