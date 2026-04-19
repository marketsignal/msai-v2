"use client";

import { use, useEffect, useState } from "react";
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
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  ArrowLeft,
  FlaskConical,
  CheckCircle2,
  BarChart3,
  TrendingUp,
  Trophy,
  Hash,
} from "lucide-react";
import { apiGet, ApiError, type StrategyResponse } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";

export default function StrategyDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const { getToken } = useAuth();
  const [strategy, setStrategy] = useState<StrategyResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [notFound, setNotFound] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [configText, setConfigText] = useState<string>("{}");
  const [validateOpen, setValidateOpen] = useState<boolean>(false);
  const [isValid, setIsValid] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const data = await apiGet<StrategyResponse>(
          `/api/v1/strategies/${encodeURIComponent(id)}`,
          token,
        );
        if (cancelled) return;
        setStrategy(data);
        setConfigText(
          data.default_config
            ? JSON.stringify(data.default_config, null, 2)
            : "{}",
        );
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          const msg =
            err instanceof ApiError
              ? `Failed to load strategy (${err.status})`
              : "Failed to load strategy";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [id, getToken]);

  function handleValidate(): void {
    try {
      JSON.parse(configText);
      setIsValid(true);
    } catch {
      setIsValid(false);
    }
    setValidateOpen(true);
  }

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center text-sm text-muted-foreground">
        Loading strategy...
      </div>
    );
  }

  if (notFound || !strategy) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">{error ?? "Strategy not found"}</p>
        <Button asChild variant="outline">
          <Link href="/strategies">Back to Strategies</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => router.push("/strategies")}
        >
          <ArrowLeft className="size-4" />
        </Button>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              {strategy.name}
            </h1>
            <Badge
              variant="secondary"
              className="bg-muted text-muted-foreground hover:bg-muted"
            >
              registered
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {strategy.description}
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Key info */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Strategy Class
            </CardTitle>
            <BarChart3 className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="truncate font-mono text-base">
              {strategy.strategy_class}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Code Hash
            </CardTitle>
            <Hash className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className="truncate font-mono text-sm"
              title={strategy.code_hash}
            >
              {strategy.code_hash.slice(0, 12)}...
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Registered
            </CardTitle>
            <Trophy className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-base">{formatDate(strategy.created_at)}</div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              File
            </CardTitle>
            <TrendingUp className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className="truncate font-mono text-xs"
              title={strategy.file_path}
            >
              {strategy.file_path}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Config editor */}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Configuration</CardTitle>
            <CardDescription>
              Default JSON configuration for this strategy
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Textarea
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
              className="h-64 font-mono text-sm"
              placeholder="Enter JSON configuration..."
            />
            <div className="flex gap-2">
              <Dialog open={validateOpen} onOpenChange={setValidateOpen}>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    className="gap-1.5"
                    onClick={handleValidate}
                  >
                    <CheckCircle2 className="size-3.5" />
                    Validate
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>
                      {isValid
                        ? "Valid Configuration"
                        : "Invalid Configuration"}
                    </DialogTitle>
                    <DialogDescription>
                      {isValid
                        ? "The JSON configuration is valid and can be used for backtesting."
                        : "The JSON configuration contains syntax errors. Please fix them and try again."}
                    </DialogDescription>
                  </DialogHeader>
                  <DialogFooter>
                    <Button onClick={() => setValidateOpen(false)}>
                      Close
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
              <Button asChild className="gap-1.5">
                <Link href={`/backtests?strategy=${strategy.id}`}>
                  <FlaskConical className="size-3.5" />
                  Run Backtest
                </Link>
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/50">
          <CardHeader>
            <CardTitle className="text-base">Schema</CardTitle>
            <CardDescription>
              Config schema declared by the strategy
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-72 overflow-auto rounded-md bg-muted/40 p-3 font-mono text-xs">
              {strategy.config_schema
                ? JSON.stringify(strategy.config_schema, null, 2)
                : "No schema declared."}
            </pre>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
