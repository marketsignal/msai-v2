"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useMutation } from "@tanstack/react-query";

import {
  postOnboard,
  postOnboardDryRun,
  type AssetClass,
  type DryRunResponse,
  type OnboardRequest,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { slugifySymbol } from "@/lib/hooks/use-symbol-mutations";

/**
 * Earliest start date Databento's EQUS.MINI dataset can serve (the v1 source
 * for equity 1m bars). Onboards with `start` before this floor return 422
 * `data_start_before_available_start` from Databento. Futures (GLBX.MDP.3)
 * and FX (IB-driven) start earlier than this date too, so this floor is
 * safe for all v1 asset classes.
 */
const PROVIDER_MIN_START = "2023-03-28";

function clampStart(candidate: string): string {
  return candidate < PROVIDER_MIN_START ? PROVIDER_MIN_START : candidate;
}

interface AddSymbolDialogProps {
  open: boolean;
  onClose: () => void;
  onSuccess: (runId: string) => void;
  defaultStart: string;
  defaultEnd: string;
}

export function AddSymbolDialog({
  open,
  onClose,
  onSuccess,
  defaultStart,
  defaultEnd,
}: AddSymbolDialogProps): React.ReactElement {
  const { getToken } = useAuth();
  const [symbol, setSymbol] = useState("");
  const [assetClass, setAssetClass] = useState<AssetClass>("equity");
  // Clamp the default start to the provider data-availability floor so the
  // documented "$0 happy path" doesn't guarantee-fail at ingest. The floor
  // is safe for all v1 asset classes; user can edit the input freely above
  // it, and the date input's `min` attribute blocks picking earlier dates.
  const [start, setStart] = useState(clampStart(defaultStart));
  const [end, setEnd] = useState(defaultEnd);
  const [estimate, setEstimate] = useState<DryRunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dryRunMutation = useMutation({
    mutationFn: async (body: OnboardRequest): Promise<DryRunResponse> => {
      const token = await getToken();
      return postOnboardDryRun(token, body);
    },
  });

  const onboardMutation = useMutation({
    mutationFn: async (body: OnboardRequest) => {
      const token = await getToken();
      return postOnboard(token, body);
    },
    onSuccess: (resp) => {
      onSuccess(resp.run_id);
      onClose();
      reset();
    },
    onError: (err) => {
      setError(String(err));
    },
  });

  function reset(): void {
    setSymbol("");
    setAssetClass("equity");
    setEstimate(null);
    setError(null);
  }

  function buildRequest(): OnboardRequest {
    return {
      watchlist_name: `ui-${slugifySymbol(symbol)}-${Date.now()}`,
      symbols: [
        { symbol: symbol.toUpperCase(), asset_class: assetClass, start, end },
      ],
    };
  }

  /**
   * Codex iter-2 review fix (P2): drop a previously-issued cost estimate
   * whenever the inputs that fed it change. Without this guard the user
   * could approve a $0 estimate for AAPL/Equity, then edit the symbol to
   * MSFT and click Confirm — defeating the cost-confirmation step. Each
   * input setter calls invalidateEstimate() on change so the dialog reverts
   * to the pre-estimate footer (Estimate-cost button).
   */
  function invalidateEstimate(): void {
    if (estimate !== null) setEstimate(null);
  }

  async function handleEstimate(): Promise<void> {
    setError(null);
    try {
      const result = await dryRunMutation.mutateAsync(buildRequest());
      setEstimate(result);
    } catch (err) {
      setError(String(err));
    }
  }

  function handleConfirm(): void {
    if (!estimate) return;
    onboardMutation.mutate(buildRequest());
  }

  const costNum = estimate ? parseFloat(estimate.estimated_cost_usd) : null;
  const isFreeBundled = costNum === 0;

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className="sm:max-w-[460px]"
        data-testid="add-symbol-dialog"
      >
        <DialogHeader>
          <DialogTitle>Add symbol</DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="symbol">Symbol</Label>
            <Input
              id="symbol"
              value={symbol}
              onChange={(e) => {
                setSymbol(e.target.value);
                invalidateEstimate();
              }}
              placeholder="AAPL"
              autoFocus
              data-testid="add-symbol-input"
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="asset-class">Asset class</Label>
            <Select
              value={assetClass}
              onValueChange={(v) => {
                setAssetClass(v as AssetClass);
                invalidateEstimate();
              }}
            >
              <SelectTrigger id="asset-class">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="equity">Equity</SelectItem>
                <SelectItem value="futures">Futures</SelectItem>
                <SelectItem value="fx">FX-futures</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <Label htmlFor="start">Start</Label>
              <Input
                id="start"
                type="date"
                min={PROVIDER_MIN_START}
                value={start}
                onChange={(e) => {
                  setStart(e.target.value);
                  invalidateEstimate();
                }}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="end">End</Label>
              <Input
                id="end"
                type="date"
                value={end}
                onChange={(e) => {
                  setEnd(e.target.value);
                  invalidateEstimate();
                }}
              />
            </div>
          </div>

          {estimate && isFreeBundled && (
            <div className="rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-sm text-emerald-400">
              $0.00 — included in your Databento plan
            </div>
          )}
          {estimate && !isFreeBundled && (
            <div className="rounded border border-sky-500/30 bg-sky-500/10 p-2 text-sm text-sky-400">
              Estimated: ${estimate.estimated_cost_usd} (
              {estimate.estimate_basis})
            </div>
          )}
          {error && (
            <div
              className="rounded border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-400"
              data-testid="add-symbol-error"
            >
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          {!estimate ? (
            <Button
              onClick={handleEstimate}
              disabled={!symbol || dryRunMutation.isPending}
            >
              Estimate cost
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => setEstimate(null)}>
                Back
              </Button>
              <Button
                onClick={handleConfirm}
                disabled={onboardMutation.isPending}
                data-testid="add-symbol-confirm"
              >
                Confirm
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
