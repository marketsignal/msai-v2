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
import { FlaskConical, Play } from "lucide-react";
import { useAuth } from "@/lib/auth";
import {
  apiGet,
  apiPost,
  ApiError,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";

interface RunBacktestFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmitted?: () => void;
}

export function RunBacktestForm({
  open,
  onOpenChange,
  onSubmitted,
}: RunBacktestFormProps): React.ReactElement {
  const { getToken } = useAuth();
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [strategiesLoading, setStrategiesLoading] = useState<boolean>(true);
  const [selectedStrategy, setSelectedStrategy] = useState<string>("");
  const [instruments, setInstruments] = useState<string>("");
  const [startDate, setStartDate] = useState<string>("2025-01-02");
  const [endDate, setEndDate] = useState<string>("2025-01-15");
  const [config, setConfig] = useState<string>("{}");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Load strategies when dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = async (): Promise<void> => {
      setStrategiesLoading(true);
      try {
        const data = await apiGet<StrategyListResponse>("/api/v1/strategies/");
        if (cancelled) return;
        setStrategies(data.items);
        if (data.items.length > 0 && !selectedStrategy) {
          setSelectedStrategy(data.items[0].id);
        }
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `Failed to load strategies (${err.status})`
            : "Failed to load strategies";
        setError(msg);
      } finally {
        if (!cancelled) setStrategiesLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const handleRunBacktest = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      // getToken returns null when MSAL has no account; apiPost will fall
      // back to the API key from NEXT_PUBLIC_MSAI_API_KEY automatically.
      const token = await getToken();
      const parsedConfig = config.trim() ? JSON.parse(config) : {};
      const parsedInstruments = instruments
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);

      await apiPost(
        "/api/v1/backtests/run",
        {
          strategy_id: selectedStrategy,
          config: parsedConfig,
          instruments: parsedInstruments,
          start_date: startDate,
          end_date: endDate,
        },
        token,
      );
      onSubmitted?.();
      onOpenChange(false);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Backtest failed to start (${err.status})`
          : err instanceof SyntaxError
            ? "Configuration JSON is invalid"
            : "Failed to start backtest";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button className="gap-1.5">
          <Play className="size-3.5" />
          Run Backtest
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Run New Backtest</DialogTitle>
          <DialogDescription>
            Configure and launch a historical backtest simulation
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label>Strategy</Label>
            <Select
              value={selectedStrategy}
              onValueChange={setSelectedStrategy}
              disabled={strategiesLoading || strategies.length === 0}
            >
              <SelectTrigger>
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
          <div className="space-y-2">
            <Label>Instruments</Label>
            <Input
              value={instruments}
              onChange={(e) => setInstruments(e.target.value)}
              placeholder="AAPL, MSFT, SPY"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Start Date</Label>
              <Input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>End Date</Label>
              <Input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label>Configuration (JSON)</Label>
            <Textarea
              value={config}
              onChange={(e) => setConfig(e.target.value)}
              className="h-32 font-mono text-sm"
              placeholder='{ "fast_period": 12, "slow_period": 26 }'
            />
          </div>
          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-400">
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
            onClick={handleRunBacktest}
            disabled={submitting || !selectedStrategy}
          >
            <FlaskConical className="size-3.5" />
            {submitting ? "Starting..." : "Run Backtest"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
