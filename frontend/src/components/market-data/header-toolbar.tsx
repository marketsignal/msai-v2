"use client";

import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Plus, Inbox } from "lucide-react";
import type { AssetClass } from "@/lib/api";
import type { WindowChoice } from "@/lib/hooks/use-inventory-query";

interface HeaderToolbarProps {
  assetClass: AssetClass | "all";
  windowChoice: WindowChoice;
  staleCount: number;
  gappedCount: number;
  activeJobsCount: number;
  onAssetClassChange: (next: AssetClass | "all") => void;
  onWindowChange: (next: WindowChoice) => void;
  onAddClick: () => void;
  onJobsClick: () => void;
  onRefreshAllStale: () => void;
  onRepairAllGaps: () => void;
}

export function HeaderToolbar(props: HeaderToolbarProps): React.ReactElement {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Market Data</h1>
        <div className="flex gap-2">
          <Button
            onClick={props.onAddClick}
            className="gap-1.5"
            data-testid="header-add-symbol"
          >
            <Plus className="size-4" /> Add symbol
          </Button>
          <Button
            variant="secondary"
            onClick={props.onJobsClick}
            className="gap-1.5"
            data-testid="header-jobs"
          >
            <Inbox className="size-4" /> Jobs{" "}
            {props.activeJobsCount > 0 ? `(${props.activeJobsCount})` : ""}
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <ToggleGroup
          type="single"
          value={props.assetClass}
          onValueChange={(v) =>
            v && props.onAssetClassChange(v as AssetClass | "all")
          }
          className="border rounded-md"
        >
          <ToggleGroupItem value="all">All</ToggleGroupItem>
          <ToggleGroupItem value="equity">Equity</ToggleGroupItem>
          <ToggleGroupItem value="futures">Futures</ToggleGroupItem>
          <ToggleGroupItem value="fx">FX</ToggleGroupItem>
        </ToggleGroup>

        <Select
          value={props.windowChoice}
          onValueChange={(v) => props.onWindowChange(v as WindowChoice)}
        >
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1y">Trailing 1y</SelectItem>
            <SelectItem value="2y">Trailing 2y</SelectItem>
            <SelectItem value="5y">Trailing 5y</SelectItem>
            <SelectItem value="10y">Trailing 10y</SelectItem>
            <SelectItem value="custom">Custom…</SelectItem>
          </SelectContent>
        </Select>

        <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
          {props.staleCount > 0 && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7"
              onClick={props.onRefreshAllStale}
            >
              {props.staleCount} stale · Refresh all
            </Button>
          )}
          {props.gappedCount > 0 && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7"
              onClick={props.onRepairAllGaps}
            >
              {props.gappedCount} gapped · Repair all
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
