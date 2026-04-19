"use client";

import { Card, CardContent } from "@/components/ui/card";

interface KpiCardProps {
  label: string;
  value: number;
  icon: React.ReactNode;
}

export function KpiCard({
  label,
  value,
  icon,
}: KpiCardProps): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardContent className="flex items-center gap-3 p-4">
        <div className="flex size-9 items-center justify-center rounded-md bg-muted">
          {icon}
        </div>
        <div>
          <p className="text-2xl font-semibold tracking-tight">{value}</p>
          <p className="text-xs text-muted-foreground">{label}</p>
        </div>
      </CardContent>
    </Card>
  );
}
