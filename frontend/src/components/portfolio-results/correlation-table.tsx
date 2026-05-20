"use client";

/**
 * CorrelationTable — sortable shadcn Table companion to the heatmap.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H5. Heatmap shows the
 * intuition; the table gives operators the exact values to copy. Cell
 * background tints (green→neutral→red) mirror the diverging scale so the
 * table still reads as a correlation matrix at a glance.
 */

import { useMemo, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export type CorrelationMatrix = Record<string, Record<string, number>>;

export interface CorrelationTableProps {
  matrix: CorrelationMatrix;
  title: string;
  description?: string;
  testId: string;
}

type SortDirection = "asc" | "desc" | null;

interface SortState {
  column: string | null;
  direction: SortDirection;
}

/**
 * Map a Pearson coefficient in [-1, 1] to a CSS color string for cell
 * background. Negative = blue, positive = red, zero = transparent. The
 * intensity scales linearly with |r| so the eye finds extreme pairs fast.
 */
function correlationCellColor(value: number): string {
  if (!Number.isFinite(value)) return "transparent";
  const intensity = Math.min(Math.abs(value), 1);
  if (value > 0) {
    return `rgba(239, 68, 68, ${intensity * 0.35})`; // red-500
  }
  if (value < 0) {
    return `rgba(59, 130, 246, ${intensity * 0.35})`; // blue-500
  }
  return "transparent";
}

export function CorrelationTable({
  matrix,
  title,
  description,
  testId,
}: CorrelationTableProps): React.ReactElement {
  const ids = useMemo(() => Object.keys(matrix), [matrix]);
  const [sort, setSort] = useState<SortState>({
    column: null,
    direction: null,
  });

  const sortedRowIds = useMemo(() => {
    if (!sort.column || !sort.direction) return ids;
    const col = sort.column;
    const dir = sort.direction === "asc" ? 1 : -1;
    return [...ids].sort((a, b) => {
      const va = matrix[a]?.[col] ?? 0;
      const vb = matrix[b]?.[col] ?? 0;
      return (va - vb) * dir;
    });
  }, [ids, matrix, sort]);

  if (ids.length < 2) {
    return (
      <Card data-testid={testId}>
        <CardHeader>
          <CardTitle className="text-base">{title}</CardTitle>
          {description ? (
            <CardDescription>{description}</CardDescription>
          ) : null}
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Correlation table needs at least 2 strategies — not available for
            this run.
          </p>
        </CardContent>
      </Card>
    );
  }

  const toggleSort = (column: string): void => {
    setSort((prev) => {
      if (prev.column !== column) return { column, direction: "desc" };
      if (prev.direction === "desc") return { column, direction: "asc" };
      return { column: null, direction: null };
    });
  };

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[120px]">Strategy</TableHead>
                {ids.map((id) => (
                  <TableHead
                    key={id}
                    className="cursor-pointer select-none text-right hover:text-foreground"
                    onClick={() => toggleSort(id)}
                  >
                    <span className="inline-flex items-center justify-end gap-1">
                      {id}
                      {sort.column === id ? (
                        <span aria-hidden="true" className="text-xs">
                          {sort.direction === "asc" ? "↑" : "↓"}
                        </span>
                      ) : null}
                    </span>
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedRowIds.map((row) => (
                <TableRow key={row}>
                  <TableCell className="font-medium">{row}</TableCell>
                  {ids.map((col) => {
                    const value = matrix[row]?.[col];
                    const safe = Number.isFinite(value) ? value : 0;
                    return (
                      <TableCell
                        key={col}
                        className="text-right font-mono text-xs tabular-nums"
                        style={{ backgroundColor: correlationCellColor(safe) }}
                        data-testid={`corr-${row}-${col}`}
                      >
                        {safe.toFixed(2)}
                      </TableCell>
                    );
                  })}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
