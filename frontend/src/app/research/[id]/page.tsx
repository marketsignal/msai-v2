"use client";

import { use, useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ArrowLeft, Trophy, CheckCircle2 } from "lucide-react";
import {
  apiGet,
  apiPost,
  ApiError,
  cancelResearchJob,
  describeApiError,
  type ResearchJobDetailResponse,
  type ResearchPromotionResponse,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
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
import { Ban } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import { statusColor, jobTypeLabel } from "@/lib/status";

function truncateJson(obj: Record<string, unknown>, maxLen: number): string {
  const str = JSON.stringify(obj);
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen) + "...";
}

function metricsSnippet(metrics: Record<string, unknown> | null): string {
  if (!metrics) return "--";
  const keys = Object.keys(metrics).slice(0, 3);
  return keys
    .map((k) => {
      const v = metrics[k];
      const formatted = typeof v === "number" ? v.toFixed(4) : String(v);
      return `${k}: ${formatted}`;
    })
    .join(", ");
}

export default function ResearchDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const { getToken } = useAuth();
  const [job, setJob] = useState<ResearchJobDetailResponse | null>(null);
  const [strategyName, setStrategyName] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [notFound, setNotFound] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [promoting, setPromoting] = useState<boolean>(false);
  const [promotionResult, setPromotionResult] =
    useState<ResearchPromotionResponse | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      const token = await getToken();
      const [jobData, strategies] = await Promise.all([
        apiGet<ResearchJobDetailResponse>(
          `/api/v1/research/jobs/${encodeURIComponent(id)}`,
          token,
        ),
        apiGet<StrategyListResponse>("/api/v1/strategies/", token),
      ]);
      setJob(jobData);
      const stratMap: Record<string, StrategyResponse> = {};
      for (const s of strategies.items) stratMap[s.id] = s;
      setStrategyName(
        stratMap[jobData.strategy_id]?.name ?? jobData.strategy_id.slice(0, 8),
      );
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
      } else {
        // iter-3 describeApiError sweep.
        setError(describeApiError(err, "Failed to load research job"));
      }
    } finally {
      setLoading(false);
    }
  }, [id, getToken]);

  // Initial load
  useEffect(() => {
    void load();
  }, [load]);

  // Poll every 3s while job is active
  useEffect(() => {
    const isActive = job?.status === "pending" || job?.status === "running";
    if (isActive) {
      pollRef.current = setInterval(() => void load(), 3000);
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [job?.status, load]);

  const handlePromote = async (): Promise<void> => {
    if (!job) return;
    setPromoting(true);
    setError(null);
    try {
      const token = await getToken();
      const result = await apiPost<ResearchPromotionResponse>(
        "/api/v1/research/promotions",
        { research_job_id: job.id },
        token,
      );
      setPromotionResult(result);
    } catch (err) {
      // iter-3 describeApiError sweep: 409/422 from /api/v1/research/
      // promotions carries the reason ("candidate not optimisation-
      // eligible") in detail; raw status code throws that away.
      setError(describeApiError(err, "Promotion failed"));
    } finally {
      setPromoting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center text-sm text-muted-foreground">
        Loading research job...
      </div>
    );
  }

  if (notFound || !job) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">
          {error ?? "Research job not found"}
        </p>
        <Button asChild variant="outline">
          <Link href="/research">Back to Research</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => router.push("/research")}
          >
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <Badge
                variant="secondary"
                className="bg-muted text-muted-foreground"
              >
                {jobTypeLabel(job.job_type)}
              </Badge>
              <h1 className="text-2xl font-semibold tracking-tight">
                {strategyName}
              </h1>
              <Badge variant="secondary" className={statusColor(job.status)}>
                {job.status}
              </Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {job.started_at
                ? `Started ${formatDateTime(job.started_at)}`
                : "Not started"}
              {job.completed_at
                ? ` \u2022 Completed ${formatDateTime(job.completed_at)}`
                : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {(job.status === "pending" || job.status === "running") && (
            <CancelJobButton
              jobId={job.id}
              onCancelled={() => {
                void load();
              }}
            />
          )}
          {job.status === "completed" && !promotionResult && (
            <Button
              className="gap-1.5"
              onClick={() => void handlePromote()}
              disabled={promoting}
            >
              <Trophy className="size-3.5" />
              {promoting ? "Promoting..." : "Promote Best Config"}
            </Button>
          )}
        </div>
      </div>

      {/* Progress bar for running jobs */}
      {job.status === "running" && (
        <div className="space-y-1">
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-blue-500 transition-all"
              style={{
                width: `${job.progress}%`,
              }}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            {job.progress_message ?? `${job.progress}% complete`}
          </p>
        </div>
      )}

      {/* Error banner */}
      {job.error_message && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {job.error_message}
        </div>
      )}

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Promotion success */}
      {promotionResult && (
        <div className="flex items-center gap-3 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-400">
          <CheckCircle2 className="size-4 shrink-0" />
          <div>
            <p>{promotionResult.message}</p>
            <Link
              href="/graduation"
              className="mt-1 inline-block text-xs underline underline-offset-2 hover:text-emerald-300"
            >
              View Graduation Pipeline
            </Link>
          </div>
        </div>
      )}

      {/* Config card */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Job Configuration</CardTitle>
          <CardDescription>
            Parameters used for this research run
          </CardDescription>
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded-md bg-muted/50 p-3 text-xs font-mono text-muted-foreground">
            {JSON.stringify(job.config, null, 2)}
          </pre>
        </CardContent>
      </Card>

      {/* Best result card */}
      {job.best_config && (
        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Best Result</CardTitle>
            <CardDescription>
              Optimal configuration found by the optimiser
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="mb-1 text-xs font-medium text-muted-foreground">
                Best Config
              </p>
              <pre className="overflow-x-auto rounded-md bg-muted/50 p-3 text-xs font-mono text-muted-foreground">
                {JSON.stringify(job.best_config, null, 2)}
              </pre>
            </div>
            {job.best_metrics && (
              <div>
                <p className="mb-1 text-xs font-medium text-muted-foreground">
                  Best Metrics
                </p>
                <pre className="overflow-x-auto rounded-md bg-muted/50 p-3 text-xs font-mono text-muted-foreground">
                  {JSON.stringify(job.best_metrics, null, 2)}
                </pre>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Trials table */}
      {job.trials.length > 0 && (
        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Trials</CardTitle>
            <CardDescription>
              {job.trials.length} trial{job.trials.length !== 1 ? "s" : ""}{" "}
              evaluated
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead className="w-16">#</TableHead>
                  <TableHead>Config</TableHead>
                  <TableHead>Objective</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Metrics</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {job.trials.map((trial) => (
                  <TableRow key={trial.id} className="border-border/50">
                    <TableCell className="font-mono text-xs">
                      {trial.trial_number}
                    </TableCell>
                    <TableCell className="max-w-48 truncate font-mono text-xs text-muted-foreground">
                      {truncateJson(trial.config, 60)}
                    </TableCell>
                    <TableCell className="font-mono text-sm">
                      {trial.objective_value != null
                        ? trial.objective_value.toFixed(4)
                        : "--"}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={statusColor(trial.status)}
                      >
                        {trial.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                      {metricsSnippet(trial.metrics)}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDateTime(trial.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function CancelJobButton({
  jobId,
  onCancelled,
}: {
  jobId: string;
  onCancelled: () => void;
}): React.ReactElement {
  const { getToken } = useAuth();
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCancel = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const token = await getToken();
      await cancelResearchJob(jobId, token);
      setOpen(false);
      onCancelled();
    } catch (err) {
      setError(describeApiError(err, "Cancel failed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="gap-1.5 text-red-400 hover:text-red-300"
          data-testid="research-cancel"
        >
          <Ban className="size-3.5" aria-hidden="true" />
          Cancel job
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Cancel this research job?</AlertDialogTitle>
          <AlertDialogDescription>
            Stops the running sweep/walk-forward. Trials already completed stay
            in history; trials in flight are abandoned. Cannot be resumed.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p
            className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
            role="alert"
          >
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>
            Keep running
          </AlertDialogCancel>
          <AlertDialogAction
            disabled={submitting}
            onClick={(e) => {
              e.preventDefault();
              void handleCancel();
            }}
            className="bg-red-500/90 text-red-50 hover:bg-red-500"
            data-testid="research-cancel-confirm"
          >
            {submitting ? "Cancelling…" : "Cancel job"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
