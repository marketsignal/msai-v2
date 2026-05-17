"use client";

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ArrowLeft,
  FlaskConical,
  CheckCircle2,
  XCircle,
  BarChart3,
  Hash,
  Trophy,
  TrendingUp,
} from "lucide-react";

import {
  apiGet,
  ApiError,
  describeApiError,
  validateStrategy,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";
import { StrategyEditForm } from "@/components/strategies/strategy-edit-form";
import { StrategyDeleteDialog } from "@/components/strategies/strategy-delete-dialog";

export default function StrategyDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const { getToken } = useAuth();
  const qc = useQueryClient();

  const strategyQuery = useQuery<StrategyResponse, ApiError | Error>({
    queryKey: ["strategy", id],
    queryFn: async (): Promise<StrategyResponse> => {
      const token = await getToken();
      return apiGet<StrategyResponse>(
        `/api/v1/strategies/${encodeURIComponent(id)}`,
        token,
      );
    },
    retry: (_count, err) => !(err instanceof ApiError && err.status === 404),
  });

  const [validateResult, setValidateResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);

  const validateMutation = useMutation({
    mutationFn: async (): Promise<{ message: string }> => {
      const token = await getToken();
      return validateStrategy(id, token);
    },
    onSuccess: (data) => {
      setValidateResult({ ok: true, message: data.message });
    },
    onError: (err) => {
      // iter-3 SF P2: replace hand-rolled detail extraction with
      // describeApiError so a structured detail object becomes JSON
      // instead of "[object Object]" (the cast to String dropped it).
      setValidateResult({
        ok: false,
        message: describeApiError(err, "Validation failed"),
      });
    },
  });

  if (strategyQuery.isPending) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (strategyQuery.isError) {
    const is404 =
      strategyQuery.error instanceof ApiError &&
      strategyQuery.error.status === 404;
    const detail = is404
      ? null
      : strategyQuery.error instanceof ApiError
        ? `HTTP ${strategyQuery.error.status} — ${strategyQuery.error.message}`
        : strategyQuery.error.message;
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">
          {is404 ? "Strategy not found" : "Failed to load strategy"}
        </p>
        {detail && (
          <p className="max-w-md font-mono text-xs text-muted-foreground/70">
            {detail}
          </p>
        )}
        <Button asChild variant="outline">
          <Link href="/strategies">Back to Strategies</Link>
        </Button>
      </div>
    );
  }

  const strategy = strategyQuery.data;

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => router.push("/strategies")}
          aria-label="Back to strategies"
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
        </Button>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {strategy.name}
            </h1>
            <Badge
              variant="secondary"
              className="bg-muted text-muted-foreground"
            >
              registered
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {strategy.description}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="gap-2"
            onClick={() => {
              setValidateResult(null);
              validateMutation.mutate();
            }}
            disabled={validateMutation.isPending}
            data-testid="strategy-validate"
          >
            <CheckCircle2 className="size-4" aria-hidden="true" />
            {validateMutation.isPending ? "Validating…" : "Validate"}
          </Button>
          <Button asChild size="sm" className="gap-2">
            <Link href={`/backtests?strategy=${strategy.id}`}>
              <FlaskConical className="size-4" aria-hidden="true" />
              Run backtest
            </Link>
          </Button>
          <StrategyDeleteDialog strategy={strategy} />
        </div>
      </div>

      {/* Key facts */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KeyFact
          label="Strategy class"
          value={strategy.strategy_class}
          icon={BarChart3}
          mono
        />
        <KeyFact
          label="Code hash"
          value={
            strategy.code_hash
              ? `${strategy.code_hash.slice(0, 12)}…`
              : "unhashed"
          }
          icon={Hash}
          mono
          title={strategy.code_hash || "unhashed"}
        />
        <KeyFact
          label="Registered"
          value={formatDate(strategy.created_at)}
          icon={Trophy}
        />
        <KeyFact
          label="File"
          value={strategy.file_path}
          icon={TrendingUp}
          mono
          truncate
        />
      </div>

      {/* Edit form + schema panel */}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Configuration</CardTitle>
            <CardDescription>
              Editable description and default config. Saves PATCH to{" "}
              <code className="font-mono">
                /api/v1/strategies/{strategy.id.slice(0, 8)}…
              </code>
              .
            </CardDescription>
          </CardHeader>
          <CardContent>
            <StrategyEditForm strategy={strategy} />
          </CardContent>
        </Card>

        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Schema</CardTitle>
            <CardDescription>
              JSON schema declared by the strategy&apos;s config class.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-96 overflow-auto rounded-md bg-muted/40 p-3 font-mono text-xs">
              {strategy.config_schema
                ? JSON.stringify(strategy.config_schema, null, 2)
                : "No schema declared."}
            </pre>
          </CardContent>
        </Card>
      </div>

      {/* Validate result dialog */}
      <Dialog
        open={validateResult !== null}
        onOpenChange={(o) => {
          if (!o) setValidateResult(null);
        }}
      >
        <DialogContent>
          {validateResult && (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2">
                  {validateResult.ok ? (
                    <CheckCircle2
                      className="size-5 text-emerald-400"
                      aria-hidden="true"
                    />
                  ) : (
                    <XCircle
                      className="size-5 text-red-400"
                      aria-hidden="true"
                    />
                  )}
                  {validateResult.ok
                    ? "Strategy validated"
                    : "Validation failed"}
                </DialogTitle>
                <DialogDescription>
                  Result from{" "}
                  <code className="font-mono">
                    POST /api/v1/strategies/{strategy.id.slice(0, 8)}…/validate
                  </code>
                </DialogDescription>
              </DialogHeader>
              <div className="rounded-md border border-border/50 bg-muted/40 p-3 font-mono text-sm">
                {validateResult.message}
              </div>
              <DialogFooter>
                <Button
                  onClick={() => {
                    setValidateResult(null);
                    if (!validateResult.ok) {
                      // Encourage re-fetch in case backend wrote new state
                      void qc.invalidateQueries({
                        queryKey: ["strategy", strategy.id],
                      });
                    }
                  }}
                >
                  Close
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function KeyFact({
  label,
  value,
  icon: Icon,
  mono,
  truncate,
  title,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
  mono?: boolean;
  truncate?: boolean;
  title?: string;
}): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {label}
        </CardTitle>
        <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
      </CardHeader>
      <CardContent>
        <div
          className={[
            "text-base",
            mono ? "font-mono text-sm" : "",
            truncate ? "truncate" : "",
          ]
            .filter(Boolean)
            .join(" ")}
          title={title ?? value}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  );
}
