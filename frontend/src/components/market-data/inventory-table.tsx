"use client";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { MoreVertical } from "lucide-react";
import { cn } from "@/lib/utils";

import type { InventoryRow } from "@/lib/api";
import { StatusBadge } from "./status-badge";

interface InventoryTableProps {
  rows: InventoryRow[];
  onRowClick: (row: InventoryRow) => void;
  onRefresh: (row: InventoryRow) => void;
  onRepair: (row: InventoryRow) => void;
  onRemove: (row: InventoryRow) => void;
  onViewChart: (row: InventoryRow) => void;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  return `${weeks}w ago`;
}

function coverageDisplay(row: InventoryRow): string {
  if (row.coverage_status === "none") return "none";
  if (!row.covered_range) return "—";
  const gapSuffix =
    row.coverage_status === "gapped"
      ? ` · ${row.missing_ranges.length} gap${row.missing_ranges.length === 1 ? "" : "s"}`
      : "";
  return `${row.covered_range}${gapSuffix}`;
}

export function InventoryTable({
  rows,
  onRowClick,
  onRefresh,
  onRepair,
  onRemove,
  onViewChart,
}: InventoryTableProps): React.ReactElement {
  return (
    <Table>
      <TableHeader className="sticky top-0 bg-background">
        <TableRow className="border-border/50 hover:bg-transparent">
          <TableHead className="w-[12%]">Symbol</TableHead>
          <TableHead className="w-[10%]">Class</TableHead>
          <TableHead className="w-[16%]">Status</TableHead>
          <TableHead className="w-[28%]">Coverage</TableHead>
          {/*
            Column shows ``last_refresh_at`` which v1 sources from
            ``InstrumentDefinition.updated_at`` — advances on any row mutation
            (alias rotation, metadata correction), not exclusively on data
            ingestion. Header reads "Last update" to avoid implying data freshness.
            Follow-up: surface a true "last successful ingest" column when the
            backend tracks it explicitly.
          */}
          <TableHead className="w-[14%]">Last update</TableHead>
          <TableHead className="w-[20%] text-right" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          // Override O-6: trust server-side is_stale; no client-side double-count.
          const stale = row.is_stale;
          return (
            <TableRow
              key={row.instrument_uid}
              data-testid={`inventory-row-${row.symbol}`}
              onClick={() => onRowClick(row)}
              className={cn(
                "cursor-pointer border-border/50",
                stale && "bg-yellow-500/[0.06]",
              )}
            >
              <TableCell className="font-mono font-medium">
                {row.symbol}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {row.asset_class}
              </TableCell>
              <TableCell>
                <StatusBadge value={row.status} />
              </TableCell>
              <TableCell className="text-muted-foreground">
                {coverageDisplay(row)}
              </TableCell>
              <TableCell
                className={cn(
                  "text-muted-foreground",
                  stale && "text-yellow-400",
                )}
              >
                {relativeTime(row.last_refresh_at)}
              </TableCell>
              <TableCell
                className="text-right"
                onClick={(e) => e.stopPropagation()}
              >
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      data-testid={`row-menu-${row.symbol}`}
                    >
                      <MoreVertical className="size-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => onRefresh(row)}>
                      Refresh
                    </DropdownMenuItem>
                    {row.coverage_status === "gapped" && (
                      <DropdownMenuItem onClick={() => onRepair(row)}>
                        Repair gaps
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onClick={() => onViewChart(row)}>
                      View chart
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={() => onRemove(row)}
                      className="text-red-400 focus:text-red-400"
                    >
                      Remove
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
