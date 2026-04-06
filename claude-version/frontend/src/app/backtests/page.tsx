"use client";

import { useState } from "react";
import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { FlaskConical, Play, ExternalLink } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";
import { backtests } from "@/lib/mock-data/backtests";
import { strategies } from "@/lib/mock-data/strategies";
import { formatPercent, formatDate } from "@/lib/format";

function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export default function BacktestsPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [instruments, setInstruments] = useState("");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");
  const [config, setConfig] = useState("{}");

  const handleRunBacktest = async (): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch(
        "/api/v1/backtests/run",
        {
          method: "POST",
          body: JSON.stringify({
            strategy_id: selectedStrategy,
            config: JSON.parse(config),
            instruments: instruments
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
            start_date: startDate,
            end_date: endDate,
          }),
        },
        token,
      );
    } catch (error) {
      console.error("Run backtest failed:", error);
    }
    setRunDialogOpen(false);
  };

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Backtests</h1>
          <p className="text-sm text-muted-foreground">
            Run and review historical strategy backtests
          </p>
        </div>
        <Dialog open={runDialogOpen} onOpenChange={setRunDialogOpen}>
          <DialogTrigger asChild>
            <Button className="gap-1.5">
              <Play className="size-3.5" />
              Run Backtest
            </Button>
          </DialogTrigger>
          <DialogContent className="max-w-lg">
            <DialogHeader>
              <DialogTitle>Run New Backtest</DialogTitle>
              <DialogDescription>
                Configure and launch a historical backtest simulation
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div className="space-y-2">
                <Label>Strategy</Label>
                <Select
                  value={selectedStrategy}
                  onValueChange={setSelectedStrategy}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select strategy..." />
                  </SelectTrigger>
                  <SelectContent>
                    {strategies.map((s) => (
                      <SelectItem key={s.id} value={s.id}>
                        {s.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Instruments</Label>
                <Input
                  value={instruments}
                  onChange={(e) => setInstruments(e.target.value)}
                  placeholder="AAPL, MSFT, SPY"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Start Date</Label>
                  <Input
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label>End Date</Label>
                  <Input
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label>Configuration (JSON)</Label>
                <Textarea
                  value={config}
                  onChange={(e) => setConfig(e.target.value)}
                  className="h-32 font-mono text-sm"
                  placeholder='{ "fast_period": 12, "slow_period": 26 }'
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setRunDialogOpen(false)}>
                Cancel
              </Button>
              <Button className="gap-1.5" onClick={handleRunBacktest}>
                <FlaskConical className="size-3.5" />
                Run Backtest
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {/* Backtests table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Backtest History</CardTitle>
          <CardDescription>All backtest runs across strategies</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Strategy</TableHead>
                <TableHead>Date Range</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Sharpe</TableHead>
                <TableHead className="text-right">Return</TableHead>
                <TableHead className="text-right">Trades</TableHead>
                <TableHead className="text-right">Run Date</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {backtests.map((bt) => (
                <TableRow key={bt.id} className="border-border/50">
                  <TableCell className="font-medium">
                    {bt.strategyName}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {bt.dateRange}
                  </TableCell>
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
                      bt.status === "completed"
                        ? bt.totalReturn >= 0
                          ? "text-emerald-500"
                          : "text-red-500"
                        : "text-muted-foreground"
                    }`}
                  >
                    {bt.status === "completed"
                      ? formatPercent(bt.totalReturn)
                      : "--"}
                  </TableCell>
                  <TableCell className="text-right">
                    {bt.status === "completed" ? bt.totalTrades : "--"}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    {formatDate(bt.runDate)}
                  </TableCell>
                  <TableCell>
                    {bt.status === "completed" && (
                      <Button asChild variant="ghost" size="icon-xs">
                        <Link href={`/backtests/${bt.id}`}>
                          <ExternalLink className="size-3.5" />
                        </Link>
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
