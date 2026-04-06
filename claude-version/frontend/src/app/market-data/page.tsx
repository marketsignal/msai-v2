"use client";

import { useState, useEffect, useMemo } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  CandlestickChart,
  type OHLCVBar,
} from "@/components/charts/candlestick-chart";
import {
  SymbolSelector,
  type SymbolOption,
} from "@/components/charts/symbol-selector";
import {
  apiGet,
  ApiError,
  type SymbolsResponse,
  type BarsResponse,
} from "@/lib/api";
import { formatCurrency, formatPercent, formatNumber } from "@/lib/format";

function formatDateForApi(date: Date): string {
  const yyyy = date.getUTCFullYear();
  const mm = String(date.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(date.getUTCDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function dateRangeForTimeframe(days: number): { start: string; end: string } {
  const end = new Date();
  const start = new Date();
  start.setUTCDate(end.getUTCDate() - days);
  return { start: formatDateForApi(start), end: formatDateForApi(end) };
}

export default function MarketDataPage(): React.ReactElement {
  const [symbols, setSymbols] = useState<SymbolOption[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("");
  const [selectedTimeframe, setSelectedTimeframe] = useState<number>(7);
  const [ohlcvData, setOhlcvData] = useState<OHLCVBar[]>([]);
  const [symbolsLoading, setSymbolsLoading] = useState<boolean>(true);
  const [barsLoading, setBarsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch available symbols on mount.
  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const data = await apiGet<SymbolsResponse>(
          "/api/v1/market-data/symbols",
        );
        if (cancelled) return;
        const flat: SymbolOption[] = [];
        for (const [assetClass, list] of Object.entries(data.symbols)) {
          for (const sym of list) {
            flat.push({
              value: sym,
              label: `${sym} - ${assetClass}`,
            });
          }
        }
        setSymbols(flat);
        if (flat.length > 0 && !selectedSymbol) {
          setSelectedSymbol(flat[0].value);
        }
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `Failed to load symbols (${err.status})`
            : "Failed to load symbols";
        setError(msg);
      } finally {
        if (!cancelled) setSymbolsLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch bars whenever symbol or timeframe changes.
  useEffect(() => {
    if (!selectedSymbol) return;
    let cancelled = false;
    const load = async (): Promise<void> => {
      setBarsLoading(true);
      setError(null);
      try {
        const { start, end } = dateRangeForTimeframe(selectedTimeframe);
        const data = await apiGet<BarsResponse>(
          `/api/v1/market-data/bars/${encodeURIComponent(
            selectedSymbol,
          )}?start=${start}&end=${end}&interval=1m`,
        );
        if (cancelled) return;
        const bars: OHLCVBar[] = data.bars.map((b) => ({
          time: b.timestamp,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
          volume: b.volume,
        }));
        setOhlcvData(bars);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `Failed to load bars (${err.status})`
            : "Failed to load bars";
        setError(msg);
        setOhlcvData([]);
      } finally {
        if (!cancelled) setBarsLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedSymbol, selectedTimeframe]);

  // Derived stats
  const stats = useMemo(() => {
    if (ohlcvData.length === 0) {
      return {
        lastPrice: null as number | null,
        priceChange: 0,
        priceChangePct: 0,
        avgVolume: 0,
        highPrice: 0,
        lowPrice: 0,
      };
    }
    const lastBar = ohlcvData[ohlcvData.length - 1];
    const firstBar = ohlcvData[0];
    const priceChange = lastBar.close - firstBar.open;
    const priceChangePct =
      firstBar.open > 0 ? (priceChange / firstBar.open) * 100 : 0;
    const totalVolume = ohlcvData.reduce((sum, bar) => sum + bar.volume, 0);
    const avgVolume = Math.round(totalVolume / ohlcvData.length);
    const highPrice = Math.max(...ohlcvData.map((b) => b.high));
    const lowPrice = Math.min(...ohlcvData.map((b) => b.low));
    return {
      lastPrice: lastBar.close,
      priceChange,
      priceChangePct,
      avgVolume,
      highPrice,
      lowPrice,
    };
  }, [ohlcvData]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Market Data</h1>
        <p className="text-sm text-muted-foreground">
          View historical price data and charts
        </p>
      </div>

      {/* Controls */}
      <SymbolSelector
        symbols={symbols}
        selectedSymbol={selectedSymbol}
        onSymbolChange={setSelectedSymbol}
        selectedTimeframe={selectedTimeframe}
        onTimeframeChange={setSelectedTimeframe}
        disabled={symbolsLoading}
      />

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Price info cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Card className="border-border/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Last Price
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {stats.lastPrice !== null
                ? formatCurrency(stats.lastPrice)
                : "--"}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Change
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                stats.priceChange >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {ohlcvData.length > 0
                ? formatPercent(stats.priceChangePct)
                : "--"}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              High
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {ohlcvData.length > 0 ? formatCurrency(stats.highPrice) : "--"}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Low
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {ohlcvData.length > 0 ? formatCurrency(stats.lowPrice) : "--"}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Avg Volume
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {ohlcvData.length > 0 ? formatNumber(stats.avgVolume) : "--"}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Candlestick chart */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">
            {selectedSymbol || "Symbol"} Price Chart
          </CardTitle>
          <CardDescription>
            OHLCV candlestick chart with volume overlay (1-minute bars)
          </CardDescription>
        </CardHeader>
        <CardContent>
          {barsLoading ? (
            <div className="flex h-[500px] items-center justify-center text-sm text-muted-foreground">
              Loading bars...
            </div>
          ) : ohlcvData.length === 0 ? (
            <div className="flex h-[500px] items-center justify-center text-sm text-muted-foreground">
              {selectedSymbol
                ? "No bars available for the selected range."
                : "Select a symbol to view its chart."}
            </div>
          ) : (
            <CandlestickChart data={ohlcvData} height={500} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
