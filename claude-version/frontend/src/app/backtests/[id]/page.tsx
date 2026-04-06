"use client";

import { use, useMemo } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Download } from "lucide-react";
import { ResultsCharts } from "@/components/backtests/results-charts";
import { TradeLog } from "@/components/backtests/trade-log";
import {
  getBacktestById,
  generateEquityCurve,
  backtestTrades,
} from "@/lib/mock-data/backtests";

export default function BacktestDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const backtest = getBacktestById(id);
  const equityCurve = useMemo(
    () => (backtest ? generateEquityCurve(backtest.totalReturn) : []),
    [backtest],
  );

  if (!backtest) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">Backtest not found</p>
        <Button asChild variant="outline">
          <Link href="/backtests">Back to Backtests</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => router.push("/backtests")}
          >
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                {backtest.strategyName} Backtest
              </h1>
              <Badge
                variant="secondary"
                className="bg-emerald-500/15 text-emerald-500"
              >
                {backtest.status}
              </Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {backtest.dateRange} &middot; {backtest.instruments.join(", ")}
            </p>
          </div>
        </div>
        <Button variant="outline" className="gap-1.5" asChild>
          <a href={`/api/v1/backtests/${backtest.id}/report`}>
            <Download className="size-3.5" />
            Download Report
          </a>
        </Button>
      </div>

      <ResultsCharts backtest={backtest} equityCurve={equityCurve} />

      <TradeLog trades={backtestTrades} />
    </div>
  );
}
