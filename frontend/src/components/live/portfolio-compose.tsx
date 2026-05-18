"use client";

import * as React from "react";
import { Plus, Snowflake } from "lucide-react";
import { toast } from "sonner";

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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/lib/auth";
import { InstrumentReadinessCheck } from "@/components/live/instrument-readiness-check";
import {
  addPortfolioMember,
  listDraftMembers,
  snapshotPortfolio,
  type LivePortfolioMember,
} from "@/lib/api/live-portfolios";
import { describeApiError } from "@/lib/api";
import type {
  LivePortfolio,
  LivePortfolioRevision,
  StrategyResponse,
} from "@/lib/api";

const CONFIG_PLACEHOLDER =
  '{"bar_type":"AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL","instruments":["AAPL.NASDAQ"],"instrument_id":"AAPL.NASDAQ"}';

interface PortfolioComposeProps {
  portfolio: LivePortfolio;
  strategies: StrategyResponse[];
  onSnapshot?: (revision: LivePortfolioRevision) => void;
}

interface AddFormState {
  strategyId: string;
  configText: string;
  instrumentsText: string;
  weightText: string;
}

const EMPTY_FORM: AddFormState = {
  strategyId: "",
  configText: "",
  instrumentsText: "",
  weightText: "1",
};

function parseConfig(text: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(text);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Config must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function parseInstruments(text: string): string[] {
  const items = text
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  if (items.length === 0) {
    throw new Error("At least one instrument is required");
  }
  return items;
}

function parseWeight(text: string): number {
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0 || n > 1) {
    throw new Error("Weight must be a number between 0 and 1");
  }
  return n;
}

function findStrategyName(
  strategies: StrategyResponse[],
  strategyId: string,
): string {
  return strategies.find((s) => s.id === strategyId)?.name ?? strategyId;
}

/**
 * UI for composing the draft revision of a `LivePortfolio`. Members are kept
 * in local state (the snapshot endpoint freezes whatever's in the draft on
 * the backend; we mirror that locally for display). The snapshot button
 * freezes the revision and hands off to a parent-supplied callback.
 */
export function PortfolioCompose({
  portfolio,
  strategies,
  onSnapshot,
}: PortfolioComposeProps): React.ReactElement {
  const { getToken } = useAuth();
  const [members, setMembers] = React.useState<LivePortfolioMember[]>([]);
  // Codex iter-7 P2: when listDraftMembers fails we cannot trust the
  // local view; the backend may have persisted members we never showed.
  // Setting this flag disables Add + Snapshot so the operator can't
  // accidentally freeze unknown content. Cleared by `setMembers` on
  // either a successful re-fetch or a successful add (which also
  // returns the canonical row).
  const [loadError, setLoadError] = React.useState<string | null>(null);
  // Codex iter-8 P2: gate Add + Snapshot on an explicit initial-load
  // state. Without this, Add was enabled while listDraftMembers was
  // still in flight — a successful add could flip members.length > 0
  // and unlock Snapshot before we ever saw the persisted members.
  const [initialLoading, setInitialLoading] = React.useState<boolean>(true);

  // Codex iter-6 P2: when the parent switches portfolios (or reload /
  // tab-restore), fetch the persisted DRAFT members so the Snapshot
  // button reflects the backend's actual draft contents.
  React.useEffect(() => {
    let cancelled = false;
    setForm(EMPTY_FORM);
    setFormError(null);
    setMembers([]);
    setLoadError(null);
    setInitialLoading(true);
    void (async (): Promise<void> => {
      try {
        const token = await getToken();
        const persisted = await listDraftMembers(portfolio.id, token);
        if (!cancelled) {
          setMembers(persisted);
          setLoadError(null);
        }
      } catch (error) {
        // Codex iter-7 P2: surface the failure + lock composition. iter-3
        // describeApiError sweep: prefer the backend's HTTPException
        // ``detail`` over the raw "GET /api/v1/.../draft failed: 503"
        // message so the operator sees the real reason.
        if (!cancelled) {
          setLoadError(
            `Failed to load existing members: ${describeApiError(error, "Server unreachable")}`,
          );
        }
      } finally {
        if (!cancelled) setInitialLoading(false);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [portfolio.id, getToken]);
  const [form, setForm] = React.useState<AddFormState>(EMPTY_FORM);
  const [formError, setFormError] = React.useState<string | null>(null);
  const [addSubmitting, setAddSubmitting] = React.useState<boolean>(false);
  const [snapshotSubmitting, setSnapshotSubmitting] =
    React.useState<boolean>(false);

  const handleAddMember = async (): Promise<void> => {
    setFormError(null);

    if (!form.strategyId) {
      setFormError("Select a strategy");
      return;
    }

    let config: Record<string, unknown>;
    let instruments: string[];
    let weight: number;
    try {
      config = parseConfig(form.configText);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Invalid JSON config";
      setFormError(`Config JSON: ${message}`);
      return;
    }
    try {
      instruments = parseInstruments(form.instrumentsText);
    } catch (error) {
      setFormError(
        error instanceof Error ? error.message : "Invalid instruments",
      );
      return;
    }
    try {
      weight = parseWeight(form.weightText);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Invalid weight");
      return;
    }

    setAddSubmitting(true);
    try {
      const token = await getToken();
      const created = await addPortfolioMember(
        portfolio.id,
        { strategy_id: form.strategyId, config, instruments, weight },
        token,
      );
      setMembers((prev) => [...prev, created]);
      setForm(EMPTY_FORM);
      toast.success("Member added to draft revision");
    } catch (error) {
      // iter-3 describeApiError sweep: backend 422 carries the schema /
      // validation reason in ``detail``; raw message is "POST /add-strategy
      // failed: 422" which is useless to the operator.
      setFormError(describeApiError(error, "Failed to add member"));
    } finally {
      setAddSubmitting(false);
    }
  };

  const handleSnapshot = async (): Promise<void> => {
    setSnapshotSubmitting(true);
    try {
      const token = await getToken();
      const revision = await snapshotPortfolio(portfolio.id, token);
      toast.success(`Revision #${revision.revision_number} frozen`);
      // Codex code-review iter-3 P2: after snapshot, the backend's
      // draft is empty (snapshot freezes whatever was there, then
      // the next `add-strategy` creates a fresh draft). Clear local
      // state so a subsequent add doesn't visually carry the now-
      // frozen members alongside the new draft's single member.
      setMembers([]);
      setForm(EMPTY_FORM);
      setFormError(null);
      onSnapshot?.(revision);
    } catch (error) {
      // iter-3 describeApiError sweep.
      toast.error(describeApiError(error, "Failed to snapshot portfolio"));
    } finally {
      setSnapshotSubmitting(false);
    }
  };

  // Codex code-review P2: removed the local-only "Remove member" action.
  // The backend has NO remove-strategy endpoint, so any local removal would
  // diverge from the server-side draft. `snapshotPortfolio` then freezes the
  // server's view (which still contains the strategy), and a real-money
  // deploy could fire against a member the operator thought they removed.
  // To recover from a mistaken add, the operator must create a new portfolio
  // — surfaced via the "Add Member error" toast guidance below.

  // Codex iter-7 + iter-8 P2: lock snapshot + add when the draft load
  // failed OR is still in flight. Either case leaves local state in an
  // unknown relationship to the server-side draft; any mutation would
  // risk freezing/appending against unverified content.
  const canSnapshot =
    members.length > 0 &&
    !snapshotSubmitting &&
    !addSubmitting &&
    loadError === null &&
    !initialLoading;
  // Codex iter-9 P2: also block Add while a snapshot is in flight —
  // after the operator clicks Snapshot, the AlertDialog confirm closes
  // but the request hasn't returned; without this gate, an Add could
  // race the snapshot and end up frozen into the revision.
  // R16 (readiness): also block Add when instruments don't resolve in
  // the registry — surface the missing/ambiguous list with onboard CTA.
  const [readinessClear, setReadinessClear] = React.useState<boolean>(true);
  const canAddMember =
    loadError === null &&
    !initialLoading &&
    !snapshotSubmitting &&
    readinessClear;

  return (
    <section className="flex flex-col gap-6 rounded-lg border border-border/50 bg-card p-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold">Compose Portfolio</h2>
        <p className="text-sm text-muted-foreground">
          Add strategy members to{" "}
          <span className="font-medium">{portfolio.name}</span> then snapshot to
          freeze a revision.
        </p>
      </header>

      {loadError ? (
        <div
          role="alert"
          data-testid="portfolio-compose-load-error"
          className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
        >
          <strong className="font-semibold">{loadError}</strong>
          <p className="mt-1 text-xs">
            Composition is locked because we cannot verify the server-side draft
            contents. Reload the page or pick a different portfolio.
          </p>
        </div>
      ) : null}

      <div className="overflow-hidden rounded-md border border-border/50">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead>Instruments</TableHead>
              <TableHead className="text-right">Weight</TableHead>
              <TableHead className="w-20" aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={4}
                  className="text-center text-sm text-muted-foreground"
                >
                  No members yet — add at least one before snapshotting.
                </TableCell>
              </TableRow>
            ) : (
              members.map((m) => (
                <TableRow
                  key={m.id}
                  data-testid={`portfolio-compose-member-row-${m.strategy_id}`}
                >
                  <TableCell className="font-medium">
                    {findStrategyName(strategies, m.strategy_id)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {m.instruments.join(", ")}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {m.weight}
                  </TableCell>
                  <TableCell className="text-right text-xs text-muted-foreground">
                    locked
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <details className="rounded-md border border-border/50 bg-background/40">
        <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium">
          Add member
        </summary>
        <div className="flex flex-col gap-4 border-t border-border/50 p-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="portfolio-compose-strategy">Strategy</Label>
            <Select
              value={form.strategyId}
              onValueChange={(value: string): void =>
                setForm((prev) => ({ ...prev, strategyId: value }))
              }
            >
              <SelectTrigger
                id="portfolio-compose-strategy"
                data-testid="portfolio-compose-strategy-select"
              >
                <SelectValue placeholder="Select a strategy" />
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

          <div className="flex flex-col gap-2">
            <Label htmlFor="portfolio-compose-config">Config (JSON)</Label>
            <Textarea
              id="portfolio-compose-config"
              rows={4}
              spellCheck={false}
              className="font-mono text-xs"
              placeholder={CONFIG_PLACEHOLDER}
              value={form.configText}
              onChange={(e): void =>
                setForm((prev) => ({ ...prev, configText: e.target.value }))
              }
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="portfolio-compose-instruments">
              Instruments (comma-separated)
            </Label>
            <Input
              id="portfolio-compose-instruments"
              placeholder="AAPL, MSFT  (or AAPL.equity to disambiguate)"
              value={form.instrumentsText}
              onChange={(e): void =>
                setForm((prev) => ({
                  ...prev,
                  instrumentsText: e.target.value,
                }))
              }
              data-testid="portfolio-compose-instruments-input"
            />
            <InstrumentReadinessCheck
              instrumentsText={form.instrumentsText}
              onValidityChange={setReadinessClear}
            />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="portfolio-compose-weight">Weight (0–1)</Label>
            <Input
              id="portfolio-compose-weight"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={form.weightText}
              onChange={(e): void =>
                setForm((prev) => ({ ...prev, weightText: e.target.value }))
              }
            />
          </div>

          {formError ? (
            <p
              role="alert"
              className="text-sm text-destructive"
              data-testid="portfolio-compose-form-error"
            >
              {formError}
            </p>
          ) : null}

          <div>
            <Button
              type="button"
              data-testid="portfolio-compose-add-member"
              disabled={addSubmitting || !canAddMember}
              onClick={handleAddMember}
              className="gap-2"
            >
              <Plus className="size-4" aria-hidden="true" />
              {addSubmitting ? "Adding…" : "Add Member"}
            </Button>
          </div>
        </div>
      </details>

      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button
            type="button"
            size="lg"
            data-testid="portfolio-compose-snapshot"
            disabled={!canSnapshot}
            className="gap-2"
          >
            <Snowflake className="size-4" aria-hidden="true" />
            {snapshotSubmitting ? "Snapshotting…" : "Snapshot Revision"}
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Snapshot this revision?</AlertDialogTitle>
            <AlertDialogDescription>
              Snapshot freezes this revision. Members cannot be added or removed
              after. The frozen revision is what live deployments reference.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={snapshotSubmitting}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="portfolio-compose-snapshot-confirm"
              disabled={snapshotSubmitting}
              onClick={handleSnapshot}
            >
              {snapshotSubmitting ? "Snapshotting…" : "Freeze Revision"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
