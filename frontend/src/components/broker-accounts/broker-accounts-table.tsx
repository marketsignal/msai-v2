"use client";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle, Plus, ServerCog } from "lucide-react";
import type { BrokerAccount } from "@/lib/api/broker-accounts";

/**
 * Broker-accounts list table (multi-account IB fleet control plane).
 *
 * Renders one row per broker account showing the IB account id, status,
 * gateway slot, trading mode, and the credential-secret VERSION (metadata
 * only — the secret itself never leaves the secrets backend, so it is never
 * shown here). Clicking a row opens the detail sheet via `onSelect`.
 *
 * All four states are handled by the parent passing `isLoading` / `error`;
 * this component renders the loading skeleton, the inline error, the empty
 * state, and the populated table.
 */
interface BrokerAccountsTableProps {
  accounts: BrokerAccount[];
  isLoading: boolean;
  /** Pre-described (via describeApiError) error message, or null. */
  error: string | null;
  /** Open the detail sheet for a given account. */
  onSelect: (account: BrokerAccount) => void;
  /** Launch the add-account flow (wizard wired in T16). */
  onAdd: () => void;
}

function statusBadgeClass(status: string): string {
  switch (status.toLowerCase()) {
    case "active":
      return "bg-emerald-500/15 text-emerald-400";
    case "archived":
      return "bg-muted text-muted-foreground";
    case "error":
    case "failed":
      return "bg-red-500/15 text-red-400";
    default:
      return "bg-amber-500/15 text-amber-400";
  }
}

function tradingModeBadgeClass(mode: string): string {
  // Live trades real money — make it visually distinct from paper.
  return mode.toLowerCase() === "live"
    ? "bg-red-500/15 text-red-400"
    : "bg-sky-500/15 text-sky-400";
}

export function BrokerAccountsTable({
  accounts,
  isLoading,
  error,
  onSelect,
  onAdd,
}: BrokerAccountsTableProps): React.ReactElement {
  if (isLoading) {
    return <TableSkeleton />;
  }

  if (error !== null) {
    return (
      <div
        className="flex items-start gap-3 rounded-md border border-red-500/30 bg-red-500/10 p-4"
        role="alert"
        data-testid="broker-accounts-error"
      >
        <AlertTriangle
          className="mt-0.5 size-5 shrink-0 text-red-400"
          aria-hidden="true"
        />
        <p className="text-sm text-red-400">
          Failed to load broker accounts:{" "}
          <span className="font-mono">{error}</span>
        </p>
      </div>
    );
  }

  if (accounts.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-4 rounded-md border border-border/50 bg-card/40 p-12 text-center"
        data-testid="broker-accounts-empty"
      >
        <ServerCog
          className="size-8 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="space-y-1">
          <p className="text-base font-medium">No broker accounts yet</p>
          <p className="max-w-md text-sm text-muted-foreground">
            Register an Interactive Brokers account to give the fleet a gateway
            slot it can trade through. Credentials are written to the secrets
            backend — never stored or shown here.
          </p>
        </div>
        <Button
          variant="default"
          size="sm"
          className="gap-2"
          onClick={onAdd}
          data-testid="broker-accounts-add-empty"
        >
          <Plus className="size-4" aria-hidden="true" />
          Add account
        </Button>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-border/50">
      <Table data-testid="broker-accounts-table">
        <TableHeader>
          <TableRow className="border-border/50 hover:bg-transparent">
            <TableHead>IB account</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Gateway slot</TableHead>
            <TableHead>Trading mode</TableHead>
            <TableHead className="text-right">Secret version</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {accounts.map((account) => (
            <TableRow
              key={account.id}
              data-testid={`broker-account-row-${account.id}`}
              className="cursor-pointer border-border/50"
              role="button"
              tabIndex={0}
              onClick={() => onSelect(account)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(account);
                }
              }}
            >
              <TableCell className="font-medium">
                <span className="font-mono">{account.ib_account_id}</span>
                {account.label ? (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {account.label}
                  </span>
                ) : null}
              </TableCell>
              <TableCell>
                <Badge
                  variant="secondary"
                  className={statusBadgeClass(account.status)}
                >
                  {account.status}
                </Badge>
              </TableCell>
              <TableCell className="font-mono">
                {account.gateway_slot}
              </TableCell>
              <TableCell>
                <Badge
                  variant="secondary"
                  className={tradingModeBadgeClass(account.trading_mode)}
                >
                  {account.trading_mode}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono text-xs text-muted-foreground">
                {account.credentials_secret_version ?? "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function TableSkeleton(): React.ReactElement {
  return (
    <div
      className="overflow-hidden rounded-md border border-border/50"
      aria-busy="true"
      data-testid="broker-accounts-loading"
    >
      <Table>
        <TableHeader>
          <TableRow className="border-border/50 hover:bg-transparent">
            <TableHead>IB account</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Gateway slot</TableHead>
            <TableHead>Trading mode</TableHead>
            <TableHead className="text-right">Secret version</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: 4 }).map((_, i) => (
            <TableRow key={i} className="border-border/50">
              <TableCell>
                <Skeleton className="h-4 w-32" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-16" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-12" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-14" />
              </TableCell>
              <TableCell className="text-right">
                <Skeleton className="ml-auto h-4 w-10" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
