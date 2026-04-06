"use client";

import { useState, useMemo } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CandlestickChart } from "@/components/charts/candlestick-chart";
import { symbols, generateOHLCV } from "@/lib/mock-data/market-data";
import { formatCurrency, formatPercent, formatNumber } from "@/lib/format";

const timeframes = [
  { label: "1D", days: 1 },
  { label: "1W", days: 7 },
  { label: "1M", days: 30 },
  { label: "3M", days: 90 },
  { label: "1Y", days: 365 },
] as const;

export default function MarketDataPage(): React.ReactElement {
  const [selectedSymbol, setSelectedSymbol] = useState("AAPL");
  const [selectedTimeframe, setSelectedTimeframe] = useState(90);

  const ohlcvData = useMemo(
    () => generateOHLCV(selectedSymbol, selectedTimeframe),
    [selectedSymbol, selectedTimeframe],
  );

  const lastBar = ohlcvData[ohlcvData.length - 1];
  const firstBar = ohlcvData[0];
  const priceChange = lastBar && firstBar ? lastBar.close - firstBar.open : 0;
  const priceChangePct =
    firstBar && firstBar.open > 0 ? (priceChange / firstBar.open) * 100 : 0;
  const totalVolume = ohlcvData.reduce((sum, bar) => sum + bar.volume, 0);
  const avgVolume =
    ohlcvData.length > 0 ? Math.round(totalVolume / ohlcvData.length) : 0;
  const highPrice = Math.max(...ohlcvData.map((b) => b.high));
  const lowPrice = Math.min(...ohlcvData.map((b) => b.low));

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
      <div className="flex flex-wrap items-center gap-4">
        <Select value={selectedSymbol} onValueChange={setSelectedSymbol}>
          <SelectTrigger className="w-56">
            <SelectValue placeholder="Select symbol..." />
          </SelectTrigger>
          <SelectContent>
            {symbols.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex gap-1">
          {timeframes.map((tf) => (
            <Button
              key={tf.label}
              variant={selectedTimeframe === tf.days ? "default" : "outline"}
              size="sm"
              onClick={() => setSelectedTimeframe(tf.days)}
            >
              {tf.label}
            </Button>
          ))}
        </div>
      </div>

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
              {lastBar ? formatCurrency(lastBar.close) : "--"}
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
                priceChange >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatPercent(priceChangePct)}
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
              {formatCurrency(highPrice)}
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
              {formatCurrency(lowPrice)}
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
              {formatNumber(avgVolume)}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Candlestick chart */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">
            {selectedSymbol} Price Chart
          </CardTitle>
          <CardDescription>
            OHLCV candlestick chart with volume overlay
          </CardDescription>
        </CardHeader>
        <CardContent>
          <CandlestickChart data={ohlcvData} height={500} />
        </CardContent>
      </Card>
    </div>
  );
}
