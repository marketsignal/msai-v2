"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
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
import {
  Clock,
  Loader2,
  CheckCircle2,
  XCircle,
  ExternalLink,
} from "lucide-react";
import { LaunchResearchForm } from "@/components/research/launch-form";
import {
  apiGet,
  ApiError,
  type ResearchJobResponse,
  type ResearchJobListResponse,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";

function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "pending":
      return "bg-amber-500/15 text-amber-500 hover:bg-amber-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    case "cancelled":
      return "bg-gray-500/15 text-gray-500 hover:bg-gray-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

function jobTypeLabel(jobType: string): string {
  switch (jobType) {
    case "parameter_sweep":
      return "Parameter Sweep";
    case "walk_forward":
      return "Walk Forward";
    default:
      return jobType;
  }
}

interface KpiCardProps {
  label: string;
  value: number;
  icon: React.ReactNode;
}

function KpiCard({ label, value, icon }: KpiCardProps): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardContent className="flex items-center gap-3 p-4">
        <div className="flex size-9 items-center justify-center rounded-md bg-muted">
          {icon}
        </div>
        <div>
          <p className="text-2xl font-semibold tracking-tight">{value}</p>
          <p className="text-xs text-muted-foreground">{label}</p>
        </div>
      </CardContent>
    </Card>
  );
}

export default function ResearchPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [launchDialogOpen, setLaunchDialogOpen] = useState<boolean>(false);
  const [jobs, setJobs] = useState<ResearchJobResponse[]>([]);
  const [strategiesById, setStrategiesById] = useState<
    Record<string, StrategyResponse>
  >({});
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      const token = await getToken();
      const [jobsData, strategies] = await Promise.all([
        apiGet<ResearchJobListResponse>("/api/v1/research/jobs", token),
        apiGet<StrategyListResponse>("/api/v1/strategies/", token),
      ]);
      setJobs(jobsData.items);
      const map: Record<string, StrategyResponse> = {};
      for (const s of strategies.items) map[s.id] = s;
      setStrategiesById(map);
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load research jobs (${err.status})`
          : "Failed to load research jobs";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  // Initial load
  useEffect(() => {
    void load();
  }, [load]);

  // Poll every 5s when there are active jobs
  useEffect(() => {
    const hasActive = jobs.some(
      (j) => j.status === "pending" || j.status === "running",
    );
    if (hasActive) {
      pollRef.current = setInterval(() => void load(), 5000);
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [jobs, load]);

  // Derive KPI counts
  const pendingCount = jobs.filter((j) => j.status === "pending").length;
  const runningCount = jobs.filter((j) => j.status === "running").length;
  const completedCount = jobs.filter((j) => j.status === "completed").length;
  const failedCount = jobs.filter((j) => j.status === "failed").length;

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Research</h1>
          <p className="text-sm text-muted-foreground">
            Parameter sweeps and walk-forward optimisation
          </p>
        </div>
        <LaunchResearchForm
          open={launchDialogOpen}
          onOpenChange={setLaunchDialogOpen}
          onSubmitted={() => void load()}
        />
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KpiCard
          label="Pending"
          value={pendingCount}
          icon={<Clock className="size-4 text-amber-500" />}
        />
        <KpiCard
          label="Running"
          value={runningCount}
          icon={<Loader2 className="size-4 text-blue-500" />}
        />
        <KpiCard
          label="Completed"
          value={completedCount}
          icon={<CheckCircle2 className="size-4 text-emerald-500" />}
        />
        <KpiCard
          label="Failed"
          value={failedCount}
          icon={<XCircle className="size-4 text-red-500" />}
        />
      </div>

      {/* Jobs table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Research Jobs</CardTitle>
          <CardDescription>
            All parameter sweep and walk-forward optimisation runs
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading research jobs...
            </div>
          ) : jobs.length === 0 ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              No research jobs yet. Click &quot;Launch Research&quot; to start
              one.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Job Type</TableHead>
                  <TableHead>Strategy</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Progress</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => {
                  const strategy = strategiesById[job.strategy_id];
                  return (
                    <TableRow key={job.id} className="border-border/50">
                      <TableCell className="font-medium">
                        {jobTypeLabel(job.job_type)}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {strategy?.name ?? job.strategy_id.slice(0, 8)}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="secondary"
                          className={statusColor(job.status)}
                        >
                          {job.status}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {job.status === "running" ? (
                          <div className="space-y-1">
                            <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
                              <div
                                className="h-full rounded-full bg-blue-500 transition-all"
                                style={{
                                  width: `${job.progress}%`,
                                }}
                              />
                            </div>
                            <p className="text-xs text-muted-foreground">
                              {job.progress_message ?? `${job.progress}%`}
                            </p>
                          </div>
                        ) : job.status === "completed" ? (
                          <span className="text-xs text-muted-foreground">
                            100%
                          </span>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            --
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground">
                        {formatDateTime(job.created_at)}
                      </TableCell>
                      <TableCell>
                        <Button asChild variant="ghost" size="icon">
                          <Link href={`/research/${job.id}`}>
                            <ExternalLink className="size-3.5" />
                          </Link>
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
