"use client";

import { useEffect, useRef } from "react";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  ColorType,
} from "lightweight-charts";
import type { OHLCVBar } from "@/lib/mock-data/market-data";

interface CandlestickChartProps {
  data: OHLCVBar[];
  height?: number;
}

export function CandlestickChart({
  data,
  height = 400,
}: CandlestickChartProps): React.ReactElement {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "hsl(0 0% 63.9%)",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "hsl(0 0% 50% / 0.08)" },
        horzLines: { color: "hsl(0 0% 50% / 0.08)" },
      },
      width: chartContainerRef.current.clientWidth,
      height,
      crosshair: {
        vertLine: { color: "hsl(0 0% 50% / 0.3)", width: 1 },
        horzLine: { color: "hsl(0 0% 50% / 0.3)", width: 1 },
      },
      rightPriceScale: {
        borderColor: "hsl(0 0% 50% / 0.1)",
      },
      timeScale: {
        borderColor: "hsl(0 0% 50% / 0.1)",
        timeVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });

    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    const handleResize = (): void => {
      if (chartContainerRef.current) {
        chart.applyOptions({
          width: chartContainerRef.current.clientWidth,
        });
      }
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [height]);

  useEffect(() => {
    if (
      !candleSeriesRef.current ||
      !volumeSeriesRef.current ||
      data.length === 0
    )
      return;

    const candleData = data.map((bar) => ({
      time: bar.time as string,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));

    const volumeData = data.map((bar) => ({
      time: bar.time as string,
      value: bar.volume,
      color:
        bar.close >= bar.open
          ? "rgba(34, 197, 94, 0.3)"
          : "rgba(239, 68, 68, 0.3)",
    }));

    candleSeriesRef.current.setData(candleData);
    volumeSeriesRef.current.setData(volumeData);

    if (chartRef.current) {
      chartRef.current.timeScale().fitContent();
    }
  }, [data]);

  return (
    <div
      ref={chartContainerRef}
      className="w-full rounded-lg"
      style={{ height }}
    />
  );
}
