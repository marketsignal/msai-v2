"use client";

import { useEffect, useState } from "react";
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
import { getMarketDataSymbols } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { StorageChart } from "@/components/data/storage-chart";
import { IngestionStatus } from "@/components/data/ingestion-status";

interface SymbolRow {
  symbol: string;
  assetClass: string;
}

export default function DataManagementPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [symbols, setSymbols] = useState<SymbolRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const data = await getMarketDataSymbols(token);
        if (cancelled) return;
        const rows: SymbolRow[] = [];
        for (const [assetClass, syms] of Object.entries(data.symbols)) {
          for (const sym of syms) {
            rows.push({ symbol: sym, assetClass });
          }
        }
        rows.sort((a, b) => a.symbol.localeCompare(b.symbol));
        setSymbols(rows);
      } catch {
        if (!cancelled) setError("Failed to load symbols");
      }
    };
    void load();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

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

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

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
          {symbols.length === 0 && !error ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              No symbols available.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Symbol</TableHead>
                  <TableHead>Asset Class</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {symbols.map((sym) => (
                  <TableRow
                    key={`${sym.assetClass}-${sym.symbol}`}
                    className="border-border/50"
                  >
                    <TableCell className="font-medium">{sym.symbol}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs font-normal">
                        {sym.assetClass}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
