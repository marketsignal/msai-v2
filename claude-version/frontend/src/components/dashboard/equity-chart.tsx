"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { formatCurrency } from "@/lib/format";

interface EquityChartProps {
  data: Array<{ date: string; value: number }>;
}

export function EquityChart({ data }: EquityChartProps): React.ReactElement {
  return (
    <Card className="border-border/50 lg:col-span-4">
      <CardHeader>
        <CardTitle className="text-base">Portfolio Performance</CardTitle>
        <CardDescription>Equity curve over the last 30 days</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={data}
              margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
            >
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop
                    offset="0%"
                    stopColor="hsl(142, 76%, 36%)"
                    stopOpacity={0.3}
                  />
                  <stop
                    offset="100%"
                    stopColor="hsl(142, 76%, 36%)"
                    stopOpacity={0}
                  />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="hsl(0 0% 50% / 0.1)"
              />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: string) => {
                  const d = new Date(v);
                  return `${d.getMonth() + 1}/${d.getDate()}`;
                }}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                domain={["dataMin - 1000", "dataMax + 1000"]}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "hsl(0 0% 12.7%)",
                  border: "1px solid hsl(0 0% 100% / 0.1)",
                  borderRadius: "8px",
                  fontSize: "12px",
                }}
                labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                formatter={(value: number | undefined) => [
                  formatCurrency(value ?? 0),
                  "Portfolio",
                ]}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke="hsl(142, 76%, 36%)"
                strokeWidth={2}
                fill="url(#equityGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
