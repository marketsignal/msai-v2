"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { FileSearch, AlertTriangle } from "lucide-react";

import {
  describeApiError,
  getLiveAudits,
  type LiveAuditsResponse,
  type LiveAuditRow,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface Props {
  deploymentId: string;
  deploymentSlug?: string;
}

/**
 * Audit log drawer — latest 50 order attempts for one deployment (R17).
 *
 * Backend `/api/v1/live/audits/{id}` returns at most 50 rows newest-first
 * with no pagination shape. The header text is explicit ("Latest 50
 * order attempts") so the user is never misled about completeness.
 */
export function AuditLogSheet({
  deploymentId,
  deploymentSlug,
}: Props): React.ReactElement {
  const [open, setOpen] = useState(false);
  const { getToken } = useAuth();

  // Lazy fetch: only run the query while the drawer is open.
  const query = useQuery<LiveAuditsResponse, Error>({
    queryKey: ["live", "audits", deploymentId],
    queryFn: async (): Promise<LiveAuditsResponse> => {
      const token = await getToken();
      return getLiveAudits(deploymentId, token);
    },
    enabled: open,
    staleTime: 5_000,
    retry: 1,
  });

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="gap-2"
          data-testid="audit-log-trigger"
        >
          <FileSearch className="size-3.5" aria-hidden="true" />
          Audit log
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full sm:max-w-3xl">
        <SheetHeader>
          <SheetTitle>Latest 50 order attempts</SheetTitle>
          <SheetDescription>
            Deployment{" "}
            <code className="font-mono">
              {deploymentSlug ?? deploymentId.slice(0, 8)}
            </code>{" "}
            — newest first, server-capped at 50.
          </SheetDescription>
        </SheetHeader>

        <div className="px-4 pb-4 pt-2">
          {query.isPending ? (
            <AuditSkeleton />
          ) : query.isError ? (
            <AuditError
              message={describeApiError(
                query.error,
                "Failed to load audit log",
              )}
            />
          ) : query.data && query.data.audits.length > 0 ? (
            <div className="overflow-hidden rounded-md border border-border/50">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-48">Timestamp</TableHead>
                    <TableHead className="w-16">Side</TableHead>
                    <TableHead>Instrument</TableHead>
                    <TableHead className="text-right">Quantity</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="w-44">Client order id</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {query.data.audits.map((row) => (
                    <AuditRow key={row.id} row={row} />
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyAudits />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function AuditRow({ row }: { row: LiveAuditRow }): React.ReactElement {
  const sideUpper = row.side.toUpperCase();
  return (
    <TableRow data-testid="audit-row">
      <TableCell className="font-mono text-xs">{row.timestamp}</TableCell>
      <TableCell>
        <Badge
          variant="secondary"
          className={
            sideUpper === "BUY"
              ? "bg-emerald-500/15 text-emerald-400"
              : "bg-red-500/15 text-red-400"
          }
        >
          {sideUpper}
        </Badge>
      </TableCell>
      <TableCell className="font-mono">{row.instrument_id}</TableCell>
      <TableCell className="text-right font-mono">{row.quantity}</TableCell>
      <TableCell>
        <Badge variant="outline">{row.status}</Badge>
      </TableCell>
      <TableCell
        className="truncate font-mono text-xs"
        title={row.client_order_id}
      >
        {row.client_order_id}
      </TableCell>
    </TableRow>
  );
}

function AuditSkeleton(): React.ReactElement {
  return (
    <div className="space-y-2" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

function AuditError({ message }: { message: string }): React.ReactElement {
  return (
    <div
      className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3"
      role="alert"
    >
      <AlertTriangle
        className="mt-0.5 size-4 shrink-0 text-red-400"
        aria-hidden="true"
      />
      <div className="text-sm text-red-400">
        Failed to load audit log: <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}

function EmptyAudits(): React.ReactElement {
  return (
    <div className="flex flex-col items-center gap-2 py-12 text-center">
      <FileSearch className="size-6 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm text-muted-foreground">
        No order attempts yet for this deployment.
      </p>
    </div>
  );
}
