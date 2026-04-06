"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { HardDrive } from "lucide-react";
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
import { storageCategories } from "@/lib/mock-data/data-management";
import { formatBytes } from "@/lib/format";

const barColors = [
  "hsl(142, 76%, 36%)",
  "hsl(217, 91%, 60%)",
  "hsl(280, 67%, 55%)",
  "hsl(38, 92%, 50%)",
  "hsl(0, 84%, 60%)",
];

export function StorageChart(): React.ReactElement {
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
                  if (v >= 1000) return [`${(v / 1000).toFixed(1)} GB`, "Size"];
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
  );
}
