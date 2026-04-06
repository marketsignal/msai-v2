"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import {
  Download,
  CheckCircle2,
  Clock,
  HardDrive,
  Database,
} from "lucide-react";
import {
  storageCategories,
  ingestionStatus,
  dataSymbols,
} from "@/lib/mock-data/data-management";
import { formatBytes, formatNumber, formatTimestamp } from "@/lib/format";

const barColors = [
  "hsl(142, 76%, 36%)",
  "hsl(217, 91%, 60%)",
  "hsl(280, 67%, 55%)",
  "hsl(38, 92%, 50%)",
  "hsl(0, 84%, 60%)",
];

function statusBadgeColor(status: string): string {
  switch (status) {
    case "success":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export default function DataManagementPage(): React.ReactElement {
  const chartData = storageCategories.map((cat) => ({
    name: cat.name,
    size: cat.sizeBytes / 1_000_000, // MB
    label: cat.label,
  }));

  const totalStorage = storageCategories.reduce(
    (sum, c) => sum + c.sizeBytes,
    0,
  );

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Data Management
          </h1>
          <p className="text-sm text-muted-foreground">
            Monitor storage, ingestion status, and manage market data
          </p>
        </div>
        <Button className="gap-1.5">
          <Download className="size-3.5" />
          Trigger Download
        </Button>
      </div>

      {/* Top row: storage chart + ingestion status */}
      <div className="grid gap-6 lg:grid-cols-5">
        {/* Storage chart */}
        <Card className="border-border/50 lg:col-span-3">
          <CardHeader>
            <div className="flex items-center gap-2">
              <HardDrive className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">Storage Usage</CardTitle>
            </div>
            <CardDescription>
              Total: {formatBytes(totalStorage)} across all asset classes
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartData}
                  margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="hsl(0 0% 50% / 0.1)"
                  />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 12, fill: "hsl(0 0% 63.9%)" }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(v: number) => {
                      if (v >= 1000) return `${(v / 1000).toFixed(1)} GB`;
                      return `${v} MB`;
                    }}
                  />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: "hsl(0 0% 12.7%)",
                      border: "1px solid hsl(0 0% 100% / 0.1)",
                      borderRadius: "8px",
                      fontSize: "12px",
                    }}
                    labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                    formatter={(value: number | undefined) => {
                      const v = value ?? 0;
                      if (v >= 1000)
                        return [`${(v / 1000).toFixed(1)} GB`, "Size"];
                      return [`${v.toFixed(0)} MB`, "Size"];
                    }}
                  />
                  <Bar dataKey="size" radius={[4, 4, 0, 0]}>
                    {chartData.map((_, index) => (
                      <Cell
                        key={`cell-${index}`}
                        fill={barColors[index % barColors.length]}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Ingestion status */}
        <Card className="border-border/50 lg:col-span-2">
          <CardHeader>
            <div className="flex items-center gap-2">
              <Database className="size-4 text-muted-foreground" />
              <CardTitle className="text-base">Ingestion Status</CardTitle>
            </div>
            <CardDescription>Automated data ingestion pipeline</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="size-4 text-emerald-500" />
                  <span className="text-sm">Status</span>
                </div>
                <Badge
                  variant="secondary"
                  className={statusBadgeColor(ingestionStatus.status)}
                >
                  {ingestionStatus.status}
                </Badge>
              </div>

              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div className="flex items-center gap-2">
                  <Clock className="size-4 text-muted-foreground" />
                  <span className="text-sm">Last Run</span>
                </div>
                <span className="text-sm text-muted-foreground">
                  {formatTimestamp(ingestionStatus.lastRun)}
                </span>
              </div>

              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <div className="flex items-center gap-2">
                  <Clock className="size-4 text-muted-foreground" />
                  <span className="text-sm">Next Scheduled</span>
                </div>
                <span className="text-sm text-muted-foreground">
                  {formatTimestamp(ingestionStatus.nextScheduled)}
                </span>
              </div>

              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Duration</span>
                <span className="text-sm font-mono text-muted-foreground">
                  {ingestionStatus.duration}
                </span>
              </div>

              <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
                <span className="text-sm">Records Processed</span>
                <span className="text-sm font-mono">
                  {formatNumber(ingestionStatus.recordsProcessed)}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Symbols table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Data Symbols</CardTitle>
          <CardDescription>
            All symbols with available market data
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Symbol</TableHead>
                <TableHead>Asset Class</TableHead>
                <TableHead>Last Updated</TableHead>
                <TableHead className="text-right">Row Count</TableHead>
                <TableHead className="text-right">Size</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {dataSymbols.map((sym) => (
                <TableRow key={sym.symbol} className="border-border/50">
                  <TableCell className="font-medium">{sym.symbol}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className="text-xs font-normal">
                      {sym.assetClass}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatTimestamp(sym.lastUpdated)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatNumber(sym.rowCount)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatBytes(sym.sizeBytes)}
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
