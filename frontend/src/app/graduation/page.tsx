"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Users,
  FileText,
  Zap,
  Archive,
  ChevronRight,
  ExternalLink,
  Loader2,
} from "lucide-react";
import {
  apiGet,
  apiPost,
  ApiError,
  describeApiError,
  type GraduationCandidateResponse,
  type GraduationCandidateListResponse,
  type GraduationTransitionResponse,
  type GraduationTransitionListResponse,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import { KpiCard } from "@/components/kpi-card";

// ---------------------------------------------------------------------------
// Stage definitions
// ---------------------------------------------------------------------------

const STAGES = [
  { key: "discovery", label: "Discovery", color: "bg-sky-500" },
  { key: "validation", label: "Validation", color: "bg-amber-500" },
  { key: "paper_candidate", label: "Paper Candidate", color: "bg-violet-500" },
  { key: "paper_running", label: "Paper Running", color: "bg-blue-500" },
  { key: "paper_review", label: "Paper Review", color: "bg-cyan-500" },
  { key: "live_candidate", label: "Live Candidate", color: "bg-emerald-500" },
  { key: "live_running", label: "Live Running", color: "bg-green-500" },
  { key: "paused", label: "Paused", color: "bg-zinc-500" },
  { key: "archived", label: "Archived", color: "bg-gray-500" },
] as const;

const VALID_TRANSITIONS: Record<string, string[]> = {
  discovery: ["validation", "archived"],
  validation: ["paper_candidate", "archived"],
  paper_candidate: ["paper_running", "archived"],
  paper_running: ["paper_review", "archived"],
  paper_review: ["live_candidate", "discovery", "archived"],
  live_candidate: ["live_running", "archived"],
  live_running: ["paused", "archived"],
  paused: ["live_running", "archived"],
  archived: [],
};

function stageLabel(key: string): string {
  return STAGES.find((s) => s.key === key)?.label ?? key;
}

function stageBadgeClass(key: string): string {
  const colorMap: Record<string, string> = {
    discovery: "bg-sky-500/15 text-sky-500",
    validation: "bg-amber-500/15 text-amber-500",
    paper_candidate: "bg-violet-500/15 text-violet-500",
    paper_running: "bg-blue-500/15 text-blue-500",
    paper_review: "bg-cyan-500/15 text-cyan-500",
    live_candidate: "bg-emerald-500/15 text-emerald-500",
    live_running: "bg-green-500/15 text-green-500",
    paused: "bg-zinc-500/15 text-zinc-400",
    archived: "bg-gray-500/15 text-gray-400",
  };
  return colorMap[key] ?? "bg-muted text-muted-foreground";
}

// ---------------------------------------------------------------------------
// Metric helpers
// ---------------------------------------------------------------------------

function metricValue(metrics: Record<string, unknown>, key: string): string {
  const v = metrics[key];
  if (v === null || v === undefined) return "--";
  if (typeof v === "number") return v.toFixed(2);
  return String(v);
}

// ---------------------------------------------------------------------------
// Candidate card (inside kanban column)
// ---------------------------------------------------------------------------

interface CandidateCardProps {
  candidate: GraduationCandidateResponse;
  strategyName: string | undefined;
  selected: boolean;
  onSelect: () => void;
}

function CandidateCard({
  candidate,
  strategyName,
  selected,
  onSelect,
}: CandidateCardProps): React.ReactElement {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full rounded-lg border p-3 text-left transition-colors ${
        selected
          ? "border-primary bg-primary/5"
          : "border-border/50 bg-card hover:border-border"
      }`}
    >
      <p className="truncate text-sm font-medium">
        {strategyName ?? candidate.strategy_id.slice(0, 8)}
      </p>
      <div className="mt-2 flex gap-3 text-xs text-muted-foreground">
        <span title="Sharpe">
          S: {metricValue(candidate.metrics, "sharpe_ratio")}
        </span>
        <span title="Return">
          R: {metricValue(candidate.metrics, "total_return")}
        </span>
        <span title="Win Rate">
          W: {metricValue(candidate.metrics, "win_rate")}
        </span>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

interface DetailPanelProps {
  candidate: GraduationCandidateResponse;
  strategyName: string | undefined;
  transitions: GraduationTransitionResponse[];
  transitionsLoading: boolean;
  transitionsError: string | null;
  onAdvance: (stage: string, reason: string) => Promise<void>;
  advancing: boolean;
  onClose: () => void;
}

function DetailPanel({
  candidate,
  strategyName,
  transitions,
  transitionsLoading,
  transitionsError,
  onAdvance,
  advancing,
  onClose,
}: DetailPanelProps): React.ReactElement {
  const [targetStage, setTargetStage] = useState<string>("");
  const [reason, setReason] = useState<string>("");
  const validTargets = VALID_TRANSITIONS[candidate.stage] ?? [];

  const handleAdvance = async (): Promise<void> => {
    if (!targetStage) return;
    await onAdvance(targetStage, reason);
    setTargetStage("");
    setReason("");
  };

  return (
    <Card className="border-border/50">
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <div className="space-y-1">
          <CardTitle className="text-base">
            {strategyName ?? candidate.strategy_id.slice(0, 8)}
          </CardTitle>
          <div className="flex items-center gap-2">
            <Badge
              variant="secondary"
              className={stageBadgeClass(candidate.stage)}
            >
              {stageLabel(candidate.stage)}
            </Badge>
            <span className="text-xs text-muted-foreground">
              Created {formatDateTime(candidate.created_at)}
            </span>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* Metrics */}
        <div>
          <h4 className="mb-2 text-sm font-medium">Metrics</h4>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {Object.entries(candidate.metrics).map(([k, v]) => (
              <div
                key={k}
                className="rounded-md border border-border/50 bg-muted/30 px-3 py-2"
              >
                <p className="text-xs text-muted-foreground">{k}</p>
                <p className="text-sm font-medium">
                  {typeof v === "number" ? v.toFixed(4) : String(v ?? "--")}
                </p>
              </div>
            ))}
          </div>
        </div>

        {/* Config */}
        <div>
          <h4 className="mb-2 text-sm font-medium">Config</h4>
          <pre className="max-h-40 overflow-auto rounded-md border border-border/50 bg-muted/30 p-3 text-xs">
            {JSON.stringify(candidate.config, null, 2)}
          </pre>
        </div>

        {/* Notes */}
        {candidate.notes && (
          <div>
            <h4 className="mb-2 text-sm font-medium">Notes</h4>
            <p className="text-sm text-muted-foreground">{candidate.notes}</p>
          </div>
        )}

        {/* Research job link */}
        {candidate.research_job_id && (
          <div>
            <Button asChild variant="outline" size="sm">
              <Link href={`/research/${candidate.research_job_id}`}>
                <ExternalLink className="mr-1.5 size-3.5" />
                View Research Job
              </Link>
            </Button>
          </div>
        )}

        <Separator className="opacity-50" />

        {/* Stage advance */}
        {validTargets.length > 0 && (
          <div className="space-y-3">
            <h4 className="text-sm font-medium">Advance Stage</h4>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div className="flex-1 space-y-1.5">
                <label
                  htmlFor="target-stage"
                  className="text-xs text-muted-foreground"
                >
                  Target stage
                </label>
                <Select value={targetStage} onValueChange={setTargetStage}>
                  <SelectTrigger id="target-stage" className="w-full">
                    <SelectValue placeholder="Select stage..." />
                  </SelectTrigger>
                  <SelectContent>
                    {validTargets.map((t) => (
                      <SelectItem key={t} value={t}>
                        {stageLabel(t)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex-1 space-y-1.5">
                <label
                  htmlFor="reason"
                  className="text-xs text-muted-foreground"
                >
                  Reason (optional)
                </label>
                <Textarea
                  id="reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Why advance?"
                  rows={1}
                  className="resize-none"
                />
              </div>
              <Button
                onClick={() => void handleAdvance()}
                disabled={!targetStage || advancing}
                size="sm"
              >
                {advancing && (
                  <Loader2 className="mr-1.5 size-3.5 animate-spin" />
                )}
                Advance Stage
              </Button>
            </div>
          </div>
        )}

        <Separator className="opacity-50" />

        {/* Transition history */}
        <div>
          <h4 className="mb-2 text-sm font-medium">Transition History</h4>
          {transitionsError ? (
            <div
              role="alert"
              className="rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-400"
            >
              {transitionsError}
            </div>
          ) : transitionsLoading ? (
            <p className="text-xs text-muted-foreground">Loading...</p>
          ) : transitions.length === 0 ? (
            <p className="text-xs text-muted-foreground">No transitions yet.</p>
          ) : (
            <div className="space-y-2">
              {transitions.map((t) => (
                <div
                  key={t.id}
                  className="flex items-center gap-2 text-xs text-muted-foreground"
                >
                  <Badge
                    variant="secondary"
                    className={`${stageBadgeClass(t.from_stage)} text-[10px]`}
                  >
                    {stageLabel(t.from_stage)}
                  </Badge>
                  <ChevronRight className="size-3" />
                  <Badge
                    variant="secondary"
                    className={`${stageBadgeClass(t.to_stage)} text-[10px]`}
                  >
                    {stageLabel(t.to_stage)}
                  </Badge>
                  <span className="ml-auto">
                    {formatDateTime(t.created_at)}
                  </span>
                  {t.reason && (
                    <span className="max-w-48 truncate" title={t.reason}>
                      — {t.reason}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function GraduationPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [candidates, setCandidates] = useState<GraduationCandidateResponse[]>(
    [],
  );
  const [strategiesById, setStrategiesById] = useState<
    Record<string, StrategyResponse>
  >({});
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transitions, setTransitions] = useState<
    GraduationTransitionResponse[]
  >([]);
  const [transitionsLoading, setTransitionsLoading] = useState<boolean>(false);
  // iter-4 SF P2: surface a transient transitions-fetch failure so the
  // empty list isn't read as "confirmed empty." Renders inline next to
  // the transitions table.
  const [transitionsError, setTransitionsError] = useState<string | null>(null);
  const [advancing, setAdvancing] = useState<boolean>(false);

  // -----------------------------------------------------------------------
  // Data loading
  // -----------------------------------------------------------------------

  const load = useCallback(async (): Promise<void> => {
    try {
      const token = await getToken();
      const [candData, strategies] = await Promise.all([
        apiGet<GraduationCandidateListResponse>(
          "/api/v1/graduation/candidates?limit=100",
          token,
        ),
        apiGet<StrategyListResponse>("/api/v1/strategies/", token),
      ]);
      setCandidates(candData.items);
      const map: Record<string, StrategyResponse> = {};
      for (const s of strategies.items) map[s.id] = s;
      setStrategiesById(map);
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load graduation data (${err.status})`
          : "Failed to load graduation data";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    void load();
  }, [load]);

  // -----------------------------------------------------------------------
  // Candidate selection + transitions
  // -----------------------------------------------------------------------

  const selectCandidate = useCallback(
    async (id: string): Promise<void> => {
      setSelectedId(id);
      setTransitions([]);
      setTransitionsError(null);
      setTransitionsLoading(true);
      try {
        const token = await getToken();
        const data = await apiGet<GraduationTransitionListResponse>(
          `/api/v1/graduation/candidates/${id}/transitions`,
          token,
        );
        setTransitions(data.items);
      } catch (err) {
        // iter-4 SF P2: previously swallowed silently with comment
        // "Non-critical — just show empty transitions." That made a
        // 503 indistinguishable from "this candidate has no
        // transitions yet." Surface the backend HTTPException detail
        // via describeApiError so the user can distinguish.
        setTransitions([]);
        setTransitionsError(
          describeApiError(err, "Failed to load transitions"),
        );
      } finally {
        setTransitionsLoading(false);
      }
    },
    [getToken],
  );

  const handleAdvance = useCallback(
    async (stage: string, reason: string): Promise<void> => {
      if (!selectedId) return;
      setAdvancing(true);
      try {
        const token = await getToken();
        await apiPost<GraduationCandidateResponse>(
          `/api/v1/graduation/candidates/${selectedId}/stage`,
          { stage, reason: reason || null },
          token,
        );
        setSelectedId(null);
        await load();
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `Stage advance failed (${err.status})`
            : "Stage advance failed";
        setError(msg);
      } finally {
        setAdvancing(false);
      }
    },
    [selectedId, getToken, load],
  );

  // -----------------------------------------------------------------------
  // Derived state
  // -----------------------------------------------------------------------

  const byStage: Record<string, GraduationCandidateResponse[]> = {};
  for (const stage of STAGES) {
    byStage[stage.key] = candidates.filter((c) => c.stage === stage.key);
  }

  const paperFlowCount =
    (byStage["paper_candidate"]?.length ?? 0) +
    (byStage["paper_running"]?.length ?? 0) +
    (byStage["paper_review"]?.length ?? 0);

  const liveFlowCount =
    (byStage["live_candidate"]?.length ?? 0) +
    (byStage["live_running"]?.length ?? 0);

  const pausedArchivedCount =
    (byStage["paused"]?.length ?? 0) + (byStage["archived"]?.length ?? 0);

  const selectedCandidate = candidates.find((c) => c.id === selectedId) ?? null;

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Graduation Pipeline
        </h1>
        <p className="text-sm text-muted-foreground">
          Strategy promotion through paper &rarr; live stages
        </p>
      </div>

      {/* Error banner */}
      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KpiCard
          label="Total Candidates"
          value={candidates.length}
          icon={<Users className="size-4 text-sky-500" />}
        />
        <KpiCard
          label="Paper Flow"
          value={paperFlowCount}
          icon={<FileText className="size-4 text-violet-500" />}
        />
        <KpiCard
          label="Live Flow"
          value={liveFlowCount}
          icon={<Zap className="size-4 text-green-500" />}
        />
        <KpiCard
          label="Paused / Archived"
          value={pausedArchivedCount}
          icon={<Archive className="size-4 text-zinc-500" />}
        />
      </div>

      {/* Kanban board */}
      {loading ? (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          Loading graduation pipeline...
        </div>
      ) : (
        <div className="overflow-x-auto pb-2">
          <div
            className="flex gap-3"
            style={{ minWidth: `${STAGES.length * 220}px` }}
          >
            {STAGES.map((stage) => {
              const items = byStage[stage.key] ?? [];
              return (
                <div
                  key={stage.key}
                  className="flex w-56 shrink-0 flex-col rounded-lg border border-border/50 bg-muted/20"
                >
                  {/* Column header */}
                  <div className="flex items-center gap-2 border-b border-border/50 px-3 py-2.5">
                    <span
                      className={`size-2.5 shrink-0 rounded-full ${stage.color}`}
                    />
                    <span className="text-xs font-medium">{stage.label}</span>
                    <Badge
                      variant="secondary"
                      className="ml-auto bg-muted text-[10px] text-muted-foreground"
                    >
                      {items.length}
                    </Badge>
                  </div>
                  {/* Column body */}
                  <div className="flex flex-1 flex-col gap-2 p-2">
                    {items.length === 0 ? (
                      <p className="px-1 py-4 text-center text-[11px] text-muted-foreground">
                        No candidates
                      </p>
                    ) : (
                      items.map((c) => (
                        <CandidateCard
                          key={c.id}
                          candidate={c}
                          strategyName={strategiesById[c.strategy_id]?.name}
                          selected={selectedId === c.id}
                          onSelect={() => void selectCandidate(c.id)}
                        />
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Detail panel */}
      {selectedCandidate && (
        <DetailPanel
          candidate={selectedCandidate}
          strategyName={strategiesById[selectedCandidate.strategy_id]?.name}
          transitions={transitions}
          transitionsLoading={transitionsLoading}
          transitionsError={transitionsError}
          onAdvance={handleAdvance}
          advancing={advancing}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  );
}
