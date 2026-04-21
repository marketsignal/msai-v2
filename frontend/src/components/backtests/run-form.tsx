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
import {
  SchemaForm,
  seedFromDefaults,
} from "@/components/strategies/schema-form";
import type { ObjectSchema } from "@/components/strategies/schema-form.types";

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

  // Structured-form state (populated when the selected strategy exposes a
  // ready JSON Schema via GET /api/v1/strategies/{id}). When the schema
  // is not ready (status != "ready"), we fall back to the JSON textarea.
  const [schemaDetail, setSchemaDetail] = useState<StrategyResponse | null>(
    null,
  );
  const [configState, setConfigState] = useState<Record<string, unknown>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

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
    // selectedStrategy is intentionally omitted: this effect is a one-shot
    // initialization that seeds the selection only when empty (line `if (!selectedStrategy)`).
    // Including it in deps would re-fetch the list whenever the user switches
    // strategies in the dropdown — wasted network + potential flicker.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, getToken]);

  // Fetch full strategy detail (incl. config_schema + default_config)
  // when the selection changes so the auto-form can render. Follows the
  // cancel-safe fetch pattern from the sibling effect above and from
  // ``frontend/src/app/strategies/[id]/page.tsx``.
  //
  // Early-return branch (no strategy selected) uses functional-update
  // identity checks so we don't produce a fresh ``{}`` on every render
  // — which would cascade into a Maximum-update-depth loop.
  useEffect(() => {
    if (!selectedStrategy) {
      setSchemaDetail((prev) => (prev === null ? prev : null));
      setConfigState((prev) => (Object.keys(prev).length === 0 ? prev : {}));
      return;
    }
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const detail = await apiGet<StrategyResponse>(
          `/api/v1/strategies/${selectedStrategy}`,
          token,
        );
        if (cancelled) return;
        setSchemaDetail(detail);
        if (detail.config_schema_status === "ready") {
          setConfigState(seedFromDefaults(detail.default_config));
          setFieldErrors({});
        } else {
          // Keep the existing JSON textarea path; seed with defaults.
          setConfig(JSON.stringify(detail.default_config ?? {}, null, 2));
        }
      } catch {
        if (cancelled) return;
        // Silent: the textarea fallback still works; error banner
        // surfaces only on submit.
        setSchemaDetail(null);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedStrategy, getToken]);

  const handleRunBacktest = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    setFieldErrors({});
    try {
      // getToken returns null when MSAL has no account; apiPost will fall
      // back to the API key from NEXT_PUBLIC_MSAI_API_KEY automatically.
      const token = await getToken();
      const formMode =
        schemaDetail?.config_schema_status === "ready" &&
        schemaDetail.config_schema;
      const parsedConfig: Record<string, unknown> = formMode
        ? configState
        : config.trim()
          ? JSON.parse(config)
          : {};
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
      // Surface 422 field-level errors from the backend validation
      // helper at api/backtests.py::_prepare_and_validate_backtest_config.
      if (err instanceof ApiError && err.status === 422) {
        const envelope = extract422Envelope(err.body);
        if (envelope) {
          const fieldMap: Record<string, string> = {};
          for (const item of envelope.details) {
            if (item.field && item.field !== "(unknown)") {
              fieldMap[item.field] = item.message;
            }
          }
          setFieldErrors(fieldMap);
          setError(envelope.message);
        } else {
          setError(`Backtest failed to start (422)`);
        }
      } else {
        const msg =
          err instanceof ApiError
            ? `Backtest failed to start (${err.status})`
            : err instanceof SyntaxError
              ? "Configuration JSON is invalid"
              : "Failed to start backtest";
        setError(msg);
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
            <Label>Configuration</Label>
            {schemaDetail?.config_schema_status === "ready" &&
            schemaDetail.config_schema ? (
              <SchemaForm
                schema={schemaDetail.config_schema as unknown as ObjectSchema}
                value={configState}
                onChange={setConfigState}
                errors={fieldErrors}
              />
            ) : (
              <>
                <Textarea
                  value={config}
                  onChange={(e) => setConfig(e.target.value)}
                  className="h-32 font-mono text-sm"
                  placeholder='{ "fast_period": 12, "slow_period": 26 }'
                />
                {schemaDetail &&
                  schemaDetail.config_schema_status !== "ready" && (
                    <p className="text-xs text-muted-foreground">
                      Auto-form unavailable for this strategy (
                      {schemaDetail.config_schema_status}). Edit JSON directly —
                      the server validates on submit.
                    </p>
                  )}
              </>
            )}
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface ValidationErrorEnvelope {
  code: string;
  message: string;
  details: { field: string; message: string }[];
}

/**
 * Pull the structured validation envelope out of an ApiError.body for a
 * 422 from ``POST /api/v1/backtests/run``. The backend produces:
 *   ``{ "detail": { "error": { code, message, details: [{field, message}] } } }``
 * via ``_prepare_and_validate_backtest_config`` in ``api/backtests.py``.
 * Returns ``null`` when the body doesn't match that shape (e.g. a
 * Pydantic-request-validation 422 from the BacktestRunRequest model,
 * which uses FastAPI's default ``{"detail": [...]}`` layout).
 */
function extract422Envelope(body: unknown): ValidationErrorEnvelope | null {
  if (!body || typeof body !== "object") return null;
  // Two shapes accepted (api-design.md transition 2026-04-21):
  //   * Preferred (top-level): { "error": { code, message, details: [...] } }
  //     — produced by main.py::_strategy_config_validation_handler.
  //   * Legacy (FastAPI HTTPException wrapper): { "detail": { "error": {...} } }
  //     — produced anywhere else that still raises `HTTPException(detail=dict)`.
  const bag = body as { error?: unknown; detail?: unknown };
  let error: unknown = bag.error;
  if (!error || typeof error !== "object") {
    const detail = bag.detail;
    if (!detail || typeof detail !== "object") return null;
    error = (detail as { error?: unknown }).error;
    if (!error || typeof error !== "object") return null;
  }
  const e = error as {
    code?: unknown;
    message?: unknown;
    details?: unknown;
  };
  if (
    typeof e.code !== "string" ||
    typeof e.message !== "string" ||
    !Array.isArray(e.details)
  ) {
    return null;
  }
  const details: { field: string; message: string }[] = [];
  for (const item of e.details) {
    if (
      item &&
      typeof item === "object" &&
      typeof (item as { field?: unknown }).field === "string" &&
      typeof (item as { message?: unknown }).message === "string"
    ) {
      details.push(item as { field: string; message: string });
    }
  }
  return { code: e.code, message: e.message, details };
}
