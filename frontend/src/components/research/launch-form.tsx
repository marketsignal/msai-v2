"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Microscope, Play } from "lucide-react";
import { useAuth } from "@/lib/auth";
import {
  apiGet,
  apiPost,
  ApiError,
  describeApiError,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";

type ResearchMode = "parameter_sweep" | "walk_forward";

function describeResearchSubmissionError(error: unknown): string {
  if (!(error instanceof ApiError) || error.status !== 422) {
    return describeApiError(error, "Failed to launch research");
  }
  const fallback = "Research inputs are invalid. Check the dates, holdout and purge settings, and try again.";
  const detail = error.body !== null && typeof error.body === "object" && "detail" in error.body
    ? error.body.detail : null;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (!Array.isArray(detail)) return fallback;

  const fieldLabels: Record<string, string> = {
    strategy_id: "Strategy", instruments: "Instruments", asset_class: "Asset Class",
    start_date: "Start Date", end_date: "End Date", objective: "Objective",
    base_config: "Base Config", parameter_grid: "Parameter Grid",
    holdout_days: "Holdout Days", holdout_fraction: "Holdout Fraction", purge_days: "Purge Days",
    train_days: "Train Days", test_days: "Test Days", step_days: "Step Days",
  };
  const messages = detail.flatMap((entry: unknown): string[] => {
    if (entry === null || typeof entry !== "object" || !("msg" in entry) || typeof entry.msg !== "string") {
      return [];
    }
    const message = entry.msg.trim().replace(/^Value error,\s*/u, "");
    if (!message) return [];
    const field = "loc" in entry && Array.isArray(entry.loc) ? entry.loc[1] : null;
    const label = typeof field === "string" ? fieldLabels[field] : undefined;
    return [label ? `${label}: ${message}` : message];
  });
  return messages.length > 0 ? messages.join(" ") : fallback;
}

interface LaunchResearchFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmitted?: () => void;
}

export function LaunchResearchForm({
  open,
  onOpenChange,
  onSubmitted,
}: LaunchResearchFormProps): React.ReactElement {
  const { getToken } = useAuth();
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [strategiesLoading, setStrategiesLoading] = useState<boolean>(true);
  const [selectedStrategy, setSelectedStrategy] = useState<string>("");
  const [mode, setMode] = useState<ResearchMode>("parameter_sweep");
  const [instruments, setInstruments] = useState<string>("");
  const [assetClass, setAssetClass] = useState<string>("stocks");
  const [startDate, setStartDate] = useState<string>("2024-01-02");
  const [endDate, setEndDate] = useState<string>("2025-01-15");
  const [objective, setObjective] = useState<string>("sharpe");
  const [baseConfig, setBaseConfig] = useState<string>("{}");
  const [paramGrid, setParamGrid] = useState<string>("{}");
  const [holdoutDays, setHoldoutDays] = useState<string>("");
  const [purgeDays, setPurgeDays] = useState<string>("5");
  // Walk-forward fields
  const [trainDays, setTrainDays] = useState<string>("252");
  const [testDays, setTestDays] = useState<string>("63");
  const [stepDays, setStepDays] = useState<string>("");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Load strategies when dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async (): Promise<void> => {
      setStrategiesLoading(true);
      try {
        const token = await getToken();
        const data = await apiGet<StrategyListResponse>(
          "/api/v1/strategies/",
          token,
        );
        if (cancelled) return;
        setStrategies(data.items);
        if (data.items.length > 0 && !selectedStrategy) {
          setSelectedStrategy(data.items[0].id);
        }
      } catch (err) {
        if (cancelled) return;
        // iter-3 describeApiError sweep.
        setError(describeApiError(err, "Failed to load strategies"));
      } finally {
        if (!cancelled) setStrategiesLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, getToken]);

  const handleSubmit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const token = await getToken();
      const parsedBaseConfig = baseConfig.trim() ? JSON.parse(baseConfig) : {};
      const parsedParamGrid = paramGrid.trim() ? JSON.parse(paramGrid) : {};
      const parsedInstruments = instruments
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);

      const endpoint =
        mode === "parameter_sweep"
          ? "/api/v1/research/sweeps"
          : "/api/v1/research/walk-forward";

      const body: Record<string, unknown> = {
        strategy_id: selectedStrategy,
        instruments: parsedInstruments,
        asset_class: assetClass,
        start_date: startDate,
        end_date: endDate,
        objective,
        base_config: parsedBaseConfig,
        parameter_grid: parsedParamGrid,
        holdout_days: holdoutDays.trim() ? Number(holdoutDays) : null,
        purge_days: Number(purgeDays),
      };

      if (mode === "walk_forward") {
        body.train_days = parseInt(trainDays, 10) || 252;
        body.test_days = parseInt(testDays, 10) || 63;
        if (stepDays.trim()) {
          body.step_days = parseInt(stepDays, 10);
        }
      }

      await apiPost(endpoint, body, token);
      onSubmitted?.();
      onOpenChange(false);
    } catch (err) {
      if (err instanceof SyntaxError) {
        setError("Configuration or parameter grid JSON is invalid");
      } else {
        setError(describeResearchSubmissionError(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button className="gap-1.5">
          <Play className="size-3.5" />
          Launch Research
        </Button>
      </DialogTrigger>
      <DialogContent className="grid-cols-1 max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Launch Research Job</DialogTitle>
          <DialogDescription>
            Configure and launch a parameter sweep or walk-forward optimisation
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          {/* Strategy */}
          <div className="space-y-2">
            <Label htmlFor="research-strategy">Strategy</Label>
            <Select
              value={selectedStrategy}
              onValueChange={setSelectedStrategy}
              disabled={strategiesLoading || strategies.length === 0}
            >
              <SelectTrigger id="research-strategy">
                <SelectValue
                  placeholder={
                    strategiesLoading
                      ? "Loading..."
                      : strategies.length === 0
                        ? "No strategies registered"
                        : "Select strategy..."
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {strategies.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Mode toggle */}
          <div className="space-y-2">
            <Label>Mode</Label>
            <div className="flex gap-2">
              <Button
                type="button"
                variant={mode === "parameter_sweep" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("parameter_sweep")}
                className="flex-1"
              >
                Parameter Sweep
              </Button>
              <Button
                type="button"
                variant={mode === "walk_forward" ? "default" : "outline"}
                size="sm"
                onClick={() => setMode("walk_forward")}
                className="flex-1"
              >
                Walk Forward
              </Button>
            </div>
          </div>

          {/* Instruments */}
          <div className="space-y-2">
            <Label htmlFor="research-instruments">Instruments</Label>
            <Input
              id="research-instruments"
              value={instruments}
              onChange={(e) => setInstruments(e.target.value)}
              placeholder="AAPL, MSFT, SPY"
            />
          </div>

          {/* Asset Class */}
          <div className="space-y-2">
            <Label htmlFor="research-asset-class">Asset Class</Label>
            <Select value={assetClass} onValueChange={setAssetClass}>
              <SelectTrigger id="research-asset-class">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="stocks">Stocks</SelectItem>
                <SelectItem value="futures">Futures</SelectItem>
                <SelectItem value="options">Options</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Date range */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="research-start">Start Date</Label>
              <Input
                id="research-start"
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="research-end">End Date</Label>
              <Input
                id="research-end"
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="research-holdout">Holdout Days (optional)</Label>
                <Input id="research-holdout" type="number" min="1" value={holdoutDays}
                  onChange={(e) => setHoldoutDays(e.target.value)} placeholder="Automatic" />
              </div>
              <div className="space-y-2">
                <Label htmlFor="research-purge">Purge Days</Label>
                <Input id="research-purge" type="number" min="0" value={purgeDays}
                  onChange={(e) => setPurgeDays(e.target.value)} />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Selection uses training only. Holdout is a separate diagnostic, with purge days
              between periods. Blank uses an automatic 20% holdout for training ranges of at
              least 252 days; shorter ranges have none. Walk-forward applies this within each
              training slice. For a four-day sweep, use holdout 2 and purge 0.
            </p>
          </div>

          {/* Objective */}
          <div className="space-y-2">
            <Label htmlFor="research-objective">Objective</Label>
            <Select value={objective} onValueChange={setObjective}>
              <SelectTrigger id="research-objective">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="sharpe">Sharpe Ratio</SelectItem>
                <SelectItem value="sortino">Sortino Ratio</SelectItem>
                <SelectItem value="total_return">Total Return</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Base Config */}
          <div className="space-y-2">
            <Label htmlFor="research-base-config">Base Config (JSON)</Label>
            <Textarea
              id="research-base-config"
              value={baseConfig}
              onChange={(e) => setBaseConfig(e.target.value)}
              className="h-20 font-mono text-sm"
              placeholder='{ "fast_period": 12 }'
            />
          </div>

          {/* Parameter Grid */}
          <div className="space-y-2">
            <Label htmlFor="research-parameter-grid">Parameter Grid (JSON)</Label>
            <Textarea
              id="research-parameter-grid"
              value={paramGrid}
              onChange={(e) => setParamGrid(e.target.value)}
              className="h-20 font-mono text-sm"
              placeholder='{ "fast_period": [8, 10, 12, 14] }'
            />
          </div>

          {/* Walk-forward specific fields */}
          {mode === "walk_forward" && (
            <div className="space-y-4 rounded-md border border-border/50 bg-muted/30 p-3">
              <p className="text-xs font-medium text-muted-foreground">
                Walk-Forward Settings
              </p>
              <div className="grid grid-cols-3 gap-3">
                <div className="space-y-2">
                  <Label htmlFor="research-train-days">Train Days</Label>
                  <Input
                    id="research-train-days"
                    type="number"
                    value={trainDays}
                    onChange={(e) => setTrainDays(e.target.value)}
                    placeholder="252"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="research-test-days">Test Days</Label>
                  <Input
                    id="research-test-days"
                    type="number"
                    value={testDays}
                    onChange={(e) => setTestDays(e.target.value)}
                    placeholder="63"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="research-step-days">Step Days</Label>
                  <Input
                    id="research-step-days"
                    type="number"
                    value={stepDays}
                    onChange={(e) => setStepDays(e.target.value)}
                    placeholder="Optional"
                  />
                </div>
              </div>
            </div>
          )}

          {error && (
            <div role="alert" className="min-w-0 break-words rounded-md border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-400">
              {error}
            </div>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            className="gap-1.5"
            onClick={() => void handleSubmit()}
            disabled={submitting || !selectedStrategy}
          >
            <Microscope className="size-3.5" />
            {submitting ? "Launching..." : "Launch Research"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
