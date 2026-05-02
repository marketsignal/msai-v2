"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { HeaderToolbar } from "@/components/market-data/header-toolbar";
import { InventoryTable } from "@/components/market-data/inventory-table";
import { RowDrawer } from "@/components/market-data/row-drawer";
import { JobsDrawer } from "@/components/market-data/jobs-drawer";
import { AddSymbolDialog } from "@/components/market-data/add-symbol-dialog";
import { EmptyState } from "@/components/market-data/empty-state";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

import {
  useInventoryQuery,
  type WindowChoice,
  windowToDateRange,
} from "@/lib/hooks/use-inventory-query";
import { useJobStatusQuery } from "@/lib/hooks/use-job-status-query";
import {
  useRefreshSymbol,
  useRemoveSymbol,
} from "@/lib/hooks/use-symbol-mutations";

import type { AssetClass, InventoryRow } from "@/lib/api";

/**
 * Headless polling component: keeps `useJobStatusQuery` alive for an active
 * run regardless of whether the Jobs drawer is open. The hook's
 * terminal-status effect invalidates the inventory query, so refresh /
 * repair / onboard mutations get reflected in the UI as soon as the worker
 * finishes — even if the user closed the drawer in the meantime.
 */
function BackgroundJobPoller({ runId }: { runId: string }): null {
  useJobStatusQuery(runId);
  return null;
}

export default function MarketDataPage(): React.ReactElement {
  const router = useRouter();
  const [assetClass, setAssetClass] = useState<AssetClass | "all">("all");
  const [windowChoice, setWindowChoice] = useState<WindowChoice>("5y");
  // Track the open drawer by instrument_uid (NOT a frozen row object) so the
  // drawer's content stays in sync with the inventory query — when a refresh
  // / repair / remove mutation invalidates inventory, the drawer re-derives
  // from the new data instead of rendering a stale snapshot.
  const [drawerInstrumentUid, setDrawerInstrumentUid] = useState<string | null>(
    null,
  );
  const [jobsOpen, setJobsOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [activeRunIds, setActiveRunIds] = useState<string[]>([]);
  const [removeTarget, setRemoveTarget] = useState<InventoryRow | null>(null);

  const { data, isLoading, error } = useInventoryQuery({
    windowChoice,
    assetClass: assetClass === "all" ? undefined : assetClass,
  });

  const drawerRow = useMemo<InventoryRow | null>(() => {
    if (drawerInstrumentUid === null || !data) return null;
    return data.find((r) => r.instrument_uid === drawerInstrumentUid) ?? null;
  }, [drawerInstrumentUid, data]);

  const { start, end } = windowToDateRange(windowChoice);
  const registerRun = (runId: string): void =>
    setActiveRunIds((prev) => (prev.includes(runId) ? prev : [runId, ...prev]));
  const refresh = useRefreshSymbol({ onRunStarted: registerRun });
  const remove = useRemoveSymbol();

  const counts = useMemo(() => {
    const rows = data ?? [];
    return {
      stale: rows.filter((r) => r.status === "stale").length,
      gapped: rows.filter((r) => r.status === "gapped").length,
    };
  }, [data]);

  const handleRefresh = (row: InventoryRow): void => {
    refresh.mutate({
      symbol: row.symbol,
      asset_class: row.asset_class,
      start,
      end,
    });
  };

  // Mutually-exclusive drawer rule: opening one closes the other
  const openDrawer = (row: InventoryRow): void => {
    setJobsOpen(false);
    setDrawerInstrumentUid(row.instrument_uid);
  };
  const closeDrawer = (): void => setDrawerInstrumentUid(null);
  const openJobs = (): void => {
    setDrawerInstrumentUid(null);
    setJobsOpen(true);
  };

  const navigateToChart = (row: InventoryRow): void => {
    router.push(`/market-data/chart?symbol=${encodeURIComponent(row.symbol)}`);
  };

  const confirmRemove = (): void => {
    if (!removeTarget) return;
    remove.mutate({
      symbol: removeTarget.symbol,
      asset_class: removeTarget.asset_class,
    });
    setRemoveTarget(null);
    closeDrawer();
  };

  return (
    <div className="space-y-6">
      <HeaderToolbar
        assetClass={assetClass}
        windowChoice={windowChoice}
        staleCount={counts.stale}
        gappedCount={counts.gapped}
        activeJobsCount={activeRunIds.length}
        onAssetClassChange={setAssetClass}
        onWindowChange={setWindowChoice}
        onAddClick={() => setAddOpen(true)}
        onJobsClick={openJobs}
        onRefreshAllStale={() => {
          (data ?? [])
            .filter((r) => r.status === "stale")
            .forEach(handleRefresh);
        }}
        onRepairAllGaps={() => {
          // Bulk repair: open the drawer for each gapped row in turn would be
          // disruptive; instead, fire a refresh covering the full window for
          // every gapped row (worker dedups inside the window).
          (data ?? [])
            .filter((r) => r.status === "gapped")
            .forEach(handleRefresh);
        }}
      />

      {error ? (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          Failed to load inventory: {String(error)}
        </div>
      ) : null}

      {isLoading ? (
        <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
          Loading inventory…
        </div>
      ) : !data || data.length === 0 ? (
        <EmptyState onAddClick={() => setAddOpen(true)} />
      ) : (
        <InventoryTable
          rows={data}
          onRowClick={openDrawer}
          onRefresh={handleRefresh}
          onRepair={openDrawer}
          onRemove={(row) => setRemoveTarget(row)}
          onViewChart={navigateToChart}
        />
      )}

      <RowDrawer
        row={drawerRow}
        recentJobs={[]}
        onClose={closeDrawer}
        onRefresh={handleRefresh}
        onRepairRange={(row, range) => {
          refresh.mutate({
            symbol: row.symbol,
            asset_class: row.asset_class,
            start: range.start,
            end: range.end,
          });
        }}
        onRemove={(row) => setRemoveTarget(row)}
        onViewChart={navigateToChart}
      />

      <JobsDrawer
        open={jobsOpen}
        activeRunIds={activeRunIds}
        onClose={() => setJobsOpen(false)}
      />

      {/* Headless pollers keep job status queries alive for each active run
          even when the Jobs drawer is closed, so terminal-status transitions
          can invalidate the inventory query and refresh the table + drawer. */}
      {activeRunIds.map((runId) => (
        <BackgroundJobPoller key={runId} runId={runId} />
      ))}

      <AddSymbolDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onSuccess={(runId) => setActiveRunIds((prev) => [runId, ...prev])}
        defaultStart={start}
        defaultEnd={end}
      />

      <AlertDialog
        open={removeTarget !== null}
        onOpenChange={(o) => !o && setRemoveTarget(null)}
      >
        <AlertDialogContent data-testid="remove-confirm-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>
              Remove {removeTarget?.symbol} from inventory?
            </AlertDialogTitle>
            <AlertDialogDescription>
              Soft-delete: the symbol disappears from your inventory but the
              underlying Parquet data is preserved. Re-onboarding restores it
              without re-paying for data. Active strategies and live deployments
              are not blocked — they continue to reference the data directly.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-red-500 hover:bg-red-600"
              onClick={confirmRemove}
              data-testid="remove-confirm-action"
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
