"use client";

import { useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Bell, AlertTriangle } from "lucide-react";
import { AlertDetailSheet } from "@/components/alerts/alert-detail-sheet";
import type { AlertRecord } from "@/lib/api";

interface Props {
  alerts: AlertRecord[];
  pageSize?: number;
}

export function AlertsTable({
  alerts,
  pageSize = 20,
}: Props): React.ReactElement {
  const [selected, setSelected] = useState<AlertRecord | null>(null);
  const [page, setPage] = useState<number>(1);

  // Snapshot the AlertRecord into local state at click time (R22) so
  // background polling cannot interrupt the sheet view.

  const totalPages = Math.max(1, Math.ceil(alerts.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const start = (safePage - 1) * pageSize;
  const visible = alerts.slice(start, start + pageSize);

  return (
    <>
      <div className="overflow-hidden rounded-md border border-border/50">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-32">Level</TableHead>
              <TableHead className="w-56">Timestamp</TableHead>
              <TableHead className="w-40">Type</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Message</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((alert, idx) => (
              <TableRow
                key={`${alert.created_at}-${start + idx}`}
                className="cursor-pointer hover:bg-accent/50"
                onClick={() => setSelected(alert)}
                data-testid="alert-row"
              >
                <TableCell>
                  <LevelBadge level={alert.level} />
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {alert.created_at}
                </TableCell>
                <TableCell>
                  <Badge variant="outline" className="font-mono text-xs">
                    {alert.type}
                  </Badge>
                </TableCell>
                <TableCell className="font-medium">{alert.title}</TableCell>
                <TableCell className="max-w-md truncate text-muted-foreground">
                  {alert.message}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {totalPages > 1 && (
        <Pagination className="mt-4">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  setPage((p) => Math.max(1, p - 1));
                }}
                aria-disabled={safePage === 1}
              />
            </PaginationItem>
            {pageNumbers(safePage, totalPages).map((n, i) => (
              <PaginationItem key={`${n}-${i}`}>
                <PaginationLink
                  href="#"
                  isActive={n === safePage}
                  onClick={(e) => {
                    e.preventDefault();
                    if (typeof n === "number") setPage(n);
                  }}
                >
                  {n}
                </PaginationLink>
              </PaginationItem>
            ))}
            <PaginationItem>
              <PaginationNext
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  setPage((p) => Math.min(totalPages, p + 1));
                }}
                aria-disabled={safePage === totalPages}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      )}

      <AlertDetailSheet alert={selected} onClose={() => setSelected(null)} />
    </>
  );
}

function pageNumbers(current: number, total: number): (number | string)[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | string)[] = [1];
  if (current > 3) pages.push("…");
  for (
    let i = Math.max(2, current - 1);
    i <= Math.min(total - 1, current + 1);
    i++
  ) {
    pages.push(i);
  }
  if (current < total - 2) pages.push("…");
  pages.push(total);
  return pages;
}

function LevelBadge({ level }: { level: string }): React.ReactElement {
  const normalized = level.toLowerCase();
  const isError = normalized === "error" || normalized === "critical";
  const isWarn = normalized === "warning";
  return (
    <Badge
      variant="secondary"
      className={
        isError
          ? "gap-1 bg-red-500/15 text-red-400"
          : isWarn
            ? "gap-1 bg-amber-500/15 text-amber-400"
            : "gap-1 bg-muted text-muted-foreground"
      }
    >
      {isError || isWarn ? (
        <AlertTriangle className="size-3" aria-hidden="true" />
      ) : (
        <Bell className="size-3" aria-hidden="true" />
      )}
      {level}
    </Badge>
  );
}
