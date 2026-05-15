"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

import { PortfolioCompose } from "@/components/live/portfolio-compose";
import { PortfolioStartDialog } from "@/components/live/portfolio-start-dialog";

import { apiGet } from "@/lib/api";
import type {
  LivePortfolio,
  LivePortfolioRevision,
  PortfolioStartResponse,
  StrategyListResponse,
  StrategyResponse,
} from "@/lib/api";
import {
  createLivePortfolio,
  listLivePortfolios,
} from "@/lib/api/live-portfolios";
import { useAuth } from "@/lib/auth";

export default function LivePortfolioPage(): React.ReactElement {
  const { getToken } = useAuth();
  const router = useRouter();

  // Codex iter-3 P2: track token-resolution separately from the token
  // value. In API-key-only dev mode (no MSAL bypass) getToken() resolves
  // to null and apiFetch falls back to NEXT_PUBLIC_MSAI_API_KEY. The
  // previous `token === null → return` guards blocked initial data
  // loads forever in that setup. Use tokenReady to decide when to fetch.
  const [token, setToken] = useState<string | null>(null);
  const [tokenReady, setTokenReady] = useState<boolean>(false);
  useEffect(() => {
    let cancelled = false;
    void (async (): Promise<void> => {
      const t = await getToken();
      if (!cancelled) {
        setToken(t);
        setTokenReady(true);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  // --- Data: portfolios + strategies ---
  const [portfolios, setPortfolios] = useState<LivePortfolio[]>([]);
  const [portfoliosError, setPortfoliosError] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<StrategyResponse[]>([]);
  const [strategiesError, setStrategiesError] = useState<string | null>(null);

  const loadPortfolios = async (
    t: string | null,
  ): Promise<LivePortfolio[] | null> => {
    try {
      const list = await listLivePortfolios(t);
      setPortfolios(list);
      setPortfoliosError(null);
      return list;
    } catch {
      setPortfoliosError("Failed to load portfolios");
      setPortfolios([]);
      return null;
    }
  };

  useEffect(() => {
    if (!tokenReady) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      const list = await loadPortfolios(token);
      if (!cancelled && list && list.length > 0 && selectedId === null) {
        setSelectedId(list[0].id);
      }
    })();
    return (): void => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, tokenReady]);

  useEffect(() => {
    if (!tokenReady) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const data = await apiGet<StrategyListResponse>(
          "/api/v1/strategies/",
          token,
        );
        if (!cancelled) {
          setStrategies(data.items);
          setStrategiesError(null);
        }
      } catch {
        if (!cancelled) {
          setStrategiesError("Failed to load strategies");
          setStrategies([]);
        }
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token, tokenReady]);

  // --- Selection ---
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedPortfolio: LivePortfolio | null =
    portfolios.find((p) => p.id === selectedId) ?? null;

  // --- Create dialog ---
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const handleCreate = async (): Promise<void> => {
    const name = createName.trim();
    if (name.length === 0) {
      setCreateError("Name is required");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createLivePortfolio(
        {
          name,
          description:
            createDescription.trim().length > 0
              ? createDescription.trim()
              : null,
        },
        token,
      );
      // Refresh list, then select the new portfolio.
      await loadPortfolios(token);
      setSelectedId(created.id);
      setCreateOpen(false);
      setCreateName("");
      setCreateDescription("");
      toast.success(`Portfolio "${created.name}" created`);
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : "Failed to create portfolio",
      );
    } finally {
      setCreating(false);
    }
  };

  // --- Snapshot → start dialog ---
  const [pendingRevision, setPendingRevision] =
    useState<LivePortfolioRevision | null>(null);
  const [startDialogOpen, setStartDialogOpen] = useState(false);

  const handleSnapshot = (revision: LivePortfolioRevision): void => {
    setPendingRevision(revision);
    setStartDialogOpen(true);
  };

  const handleDeploySuccess = (result: PortfolioStartResponse): void => {
    toast.success(`Portfolio deployed — status: ${result.status}`);
    setStartDialogOpen(false);
    setPendingRevision(null);
    router.push("/live-trading");
  };

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          Live Portfolio Deploy
        </h1>
        <p className="text-sm text-muted-foreground">
          Compose a portfolio of strategies, snapshot the revision, and deploy
          to a live account.
        </p>
        <p className="text-xs uppercase tracking-wider text-muted-foreground/80">
          API-first · deploy-with-binding-verification
        </p>
      </header>

      {/* Portfolio selector */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Portfolio</CardTitle>
          <CardDescription>
            Select an existing portfolio or create a new one.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {portfoliosError && (
            <div
              role="alert"
              className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
            >
              {portfoliosError}
            </div>
          )}
          {strategiesError && (
            <div
              role="alert"
              className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
            >
              {strategiesError}
            </div>
          )}
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="portfolio-select">Existing portfolios</Label>
              <Select
                value={selectedId ?? ""}
                onValueChange={(v): void => setSelectedId(v)}
                disabled={portfolios.length === 0}
              >
                <SelectTrigger
                  id="portfolio-select"
                  data-testid="live-portfolio-page-portfolio-select"
                >
                  <SelectValue
                    placeholder={
                      portfolios.length === 0
                        ? "No portfolios yet — create one"
                        : "Select a portfolio…"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {portfolios.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={(): void => setCreateOpen(true)}
              data-testid="live-portfolio-page-create-new"
            >
              Create new
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Active portfolio compose surface */}
      {selectedPortfolio ? (
        <PortfolioCompose
          portfolio={selectedPortfolio}
          strategies={strategies}
          onSnapshot={handleSnapshot}
        />
      ) : (
        <Card className="border-dashed border-border/50">
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            Select or create a portfolio to begin composing strategies.
          </CardContent>
        </Card>
      )}

      {/* Create-new dialog */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New live portfolio</DialogTitle>
            <DialogDescription>
              Portfolios contain one or more strategies. You can snapshot and
              deploy after composing.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="new-portfolio-name">Name</Label>
              <Input
                id="new-portfolio-name"
                value={createName}
                onChange={(e): void => setCreateName(e.target.value)}
                placeholder="e.g. EMA Cross — AAPL/SPY"
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="new-portfolio-description">
                Description (optional)
              </Label>
              <Textarea
                id="new-portfolio-description"
                value={createDescription}
                onChange={(e): void => setCreateDescription(e.target.value)}
                placeholder="What is this portfolio for?"
                rows={3}
              />
            </div>
            {createError && (
              <div
                role="alert"
                className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
              >
                {createError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={(): void => setCreateOpen(false)}
              disabled={creating}
            >
              Cancel
            </Button>
            <Button
              type="button"
              onClick={(): void => {
                void handleCreate();
              }}
              disabled={creating}
            >
              {creating ? "Creating…" : "Create portfolio"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Deploy dialog (T6) */}
      {pendingRevision && (
        <PortfolioStartDialog
          open={startDialogOpen}
          revision={pendingRevision}
          onOpenChange={(o): void => {
            setStartDialogOpen(o);
            if (!o) setPendingRevision(null);
          }}
          onSuccess={handleDeploySuccess}
        />
      )}
    </main>
  );
}
