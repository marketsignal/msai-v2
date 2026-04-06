"use client";

import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  type CandlestickData,
  type IChartApi,
} from "lightweight-charts";

type Props = {
  data: CandlestickData[];
};

export function CandlestickChart({ data }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;

    const chart: IChartApi = createChart(ref.current, {
      layout: { background: { color: "#0b1220" }, textColor: "#94a3b8" },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.08)" },
        horzLines: { color: "rgba(255,255,255,0.08)" },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      width: ref.current.clientWidth,
      height: 420,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });
    series.setData(data);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      chart.applyOptions({ width: entry.contentRect.width });
    });
    observer.observe(ref.current);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [data]);

  return <div ref={ref} className="w-full overflow-hidden rounded-xl border border-white/10" />;
}
