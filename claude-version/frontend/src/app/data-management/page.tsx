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
import { Download } from "lucide-react";
import { dataSymbols } from "@/lib/mock-data/data-management";
import { formatBytes, formatNumber, formatTimestamp } from "@/lib/format";
import { StorageChart } from "@/components/data/storage-chart";
import { IngestionStatus } from "@/components/data/ingestion-status";

export default function DataManagementPage(): React.ReactElement {
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
        <StorageChart />
        <IngestionStatus />
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
