"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  ArrowLeft,
  FlaskConical,
  CheckCircle2,
  BarChart3,
  TrendingUp,
  Trophy,
  Percent,
} from "lucide-react";
import { getStrategyById } from "@/lib/mock-data/strategies";
import { formatPercent, formatDate } from "@/lib/format";

function statusColor(status: string): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "bg-muted text-muted-foreground hover:bg-muted";
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export default function StrategyDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const strategy = getStrategyById(id);
  const [configText, setConfigText] = useState(
    strategy ? JSON.stringify(strategy.config, null, 2) : "{}",
  );
  const [validateOpen, setValidateOpen] = useState(false);
  const [isValid, setIsValid] = useState<boolean | null>(null);

  if (!strategy) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">Strategy not found</p>
        <Button asChild variant="outline">
          <Link href="/strategies">Back to Strategies</Link>
        </Button>
      </div>
    );
  }

  function handleValidate(): void {
    try {
      JSON.parse(configText);
      setIsValid(true);
    } catch {
      setIsValid(false);
    }
    setValidateOpen(true);
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => router.push("/strategies")}
        >
          <ArrowLeft className="size-4" />
        </Button>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {strategy.name}
            </h1>
            <Badge variant="secondary" className={statusColor(strategy.status)}>
              {strategy.status}
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {strategy.description}
          </p>
        </div>
      </div>

      {/* Key metrics */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Sharpe Ratio
            </CardTitle>
            <BarChart3 className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {strategy.sharpeRatio.toFixed(2)}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Return
            </CardTitle>
            <TrendingUp className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                strategy.totalReturn >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatPercent(strategy.totalReturn)}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Win Rate
            </CardTitle>
            <Trophy className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {strategy.winRate.toFixed(1)}%
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Instruments
            </CardTitle>
            <Percent className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-1.5">
              {strategy.instruments.map((inst) => (
                <Badge
                  key={inst}
                  variant="outline"
                  className="text-xs font-normal"
                >
                  {inst}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Config editor + Backtest history */}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Configuration</CardTitle>
            <CardDescription>
              Edit the JSON configuration for this strategy
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Textarea
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
              className="h-64 font-mono text-sm"
              placeholder="Enter JSON configuration..."
            />
            <div className="flex gap-2">
              <Dialog open={validateOpen} onOpenChange={setValidateOpen}>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    className="gap-1.5"
                    onClick={handleValidate}
                  >
                    <CheckCircle2 className="size-3.5" />
                    Validate
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>
                      {isValid
                        ? "Valid Configuration"
                        : "Invalid Configuration"}
                    </DialogTitle>
                    <DialogDescription>
                      {isValid
                        ? "The JSON configuration is valid and can be used for backtesting."
                        : "The JSON configuration contains syntax errors. Please fix them and try again."}
                    </DialogDescription>
                  </DialogHeader>
                  <DialogFooter>
                    <Button onClick={() => setValidateOpen(false)}>
                      Close
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
              <Button asChild className="gap-1.5">
                <Link href={`/backtests?strategy=${strategy.id}`}>
                  <FlaskConical className="size-3.5" />
                  Run Backtest
                </Link>
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Backtest History</CardTitle>
            <CardDescription>
              Previous backtest runs for this strategy
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Date Range</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Sharpe</TableHead>
                  <TableHead className="text-right">Return</TableHead>
                  <TableHead className="text-right">Run Date</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {strategy.backtestHistory.map((bt) => (
                  <TableRow key={bt.id} className="border-border/50">
                    <TableCell className="text-sm">{bt.dateRange}</TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={statusColor(bt.status)}
                      >
                        {bt.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {bt.status === "completed"
                        ? bt.sharpeRatio.toFixed(2)
                        : "--"}
                    </TableCell>
                    <TableCell
                      className={`text-right ${
                        bt.totalReturn >= 0
                          ? "text-emerald-500"
                          : "text-red-500"
                      }`}
                    >
                      {bt.status === "completed"
                        ? formatPercent(bt.totalReturn)
                        : "--"}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDate(bt.runDate)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
