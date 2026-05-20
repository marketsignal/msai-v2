"use client";

/**
 * Portfolio composition — form-based, no JSON textarea.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H4. Wires the four
 * `portfolio-compose/*` components and POSTs to `/api/v1/portfolios` via
 * the `strategy_ids` bridge (Task F1c — backend auto-creates default
 * GraduationCandidate per strategy so the user never sees that model).
 *
 * Hard PRD requirement (§ Constraints "no JSON in compose"): this page
 * MUST NOT contain a `<Textarea>` for portfolio config.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { StrategyMultiSelect } from "@/components/portfolio-compose/strategy-multi-select";
import { AllocatorSelect } from "@/components/portfolio-compose/allocator-select";
import { ObjectiveSelect } from "@/components/portfolio-compose/objective-select";
import {
  SafetyCapsForm,
  type SafetyCaps,
} from "@/components/portfolio-compose/safety-caps-form";

import {
  apiGet,
  apiPost,
  describeApiError,
  type PortfolioResponse,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface CreatePortfolioBody {
  name: string;
  objective: string;
  base_capital: number;
  requested_leverage: number;
  max_position_size: number;
  max_drawdown_halt: number;
  default_mode: "quick" | "full";
  allocator_name: string;
  /**
   * Bridge per Task F1c — backend auto-creates the default GraduationCandidate
   * per strategy so the form UX never has to surface that model (PRD US-001).
   */
  strategy_ids: string[];
}

export default function NewPortfolioPage(): React.ReactElement {
  const router = useRouter();
  const { getToken } = useAuth();

  const strategiesQuery = useQuery<StrategyResponse[], Error>({
    queryKey: ["strategies"],
    queryFn: async (): Promise<StrategyResponse[]> => {
      const token = await getToken();
      const data = await apiGet<StrategyListResponse>(
        "/api/v1/strategies/",
        token,
      );
      return data.items;
    },
  });

  const [name, setName] = useState<string>("");
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [allocator, setAllocator] = useState<string>("equal_weight");
  const [objective, setObjective] = useState<string>("maximize_sharpe");
  const [caps, setCaps] = useState<SafetyCaps>({
    max_leverage: 1.0,
    max_position_size: 0.25,
    max_drawdown_halt: 0.2,
  });
  const [capital, setCapital] = useState<number>(100_000);
  const [submitting, setSubmitting] = useState<boolean>(false);

  const valid =
    name.trim().length > 0 &&
    memberIds.length > 0 &&
    capital > 0 &&
    caps.max_leverage > 0 &&
    caps.max_position_size > 0 &&
    caps.max_drawdown_halt > 0;

  const strategies = strategiesQuery.data ?? [];
  const strategyLoadError = strategiesQuery.error;

  const onSubmit = async (): Promise<void> => {
    if (!valid || submitting) return;
    setSubmitting(true);
    try {
      const token = await getToken();
      const body: CreatePortfolioBody = {
        name: name.trim(),
        objective,
        base_capital: capital,
        requested_leverage: caps.max_leverage,
        max_position_size: caps.max_position_size,
        max_drawdown_halt: caps.max_drawdown_halt,
        default_mode: "quick",
        allocator_name: allocator,
        strategy_ids: memberIds,
      };
      const created = await apiPost<PortfolioResponse>(
        "/api/v1/portfolios",
        body,
        token,
      );
      toast.success("Portfolio saved.");
      router.push(`/portfolio/${created.id}`);
    } catch (err) {
      toast.error(describeApiError(err, "Save failed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="container mx-auto max-w-3xl space-y-6 py-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">New Portfolio</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Compose a multi-strategy portfolio. Pick members, allocator,
          objective, and safety caps — no JSON required.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Composition</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-1.5">
            <Label htmlFor="portfolio-name">Name</Label>
            <Input
              id="portfolio-name"
              data-testid="portfolio-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Diversified EMA Cross"
            />
          </div>

          <div className="space-y-1.5">
            <Label>Strategies</Label>
            {strategyLoadError ? (
              <p className="text-sm text-destructive">
                Failed to load strategies:{" "}
                {describeApiError(strategyLoadError, "unknown error")}
              </p>
            ) : (
              <StrategyMultiSelect
                options={strategies.map((s) => ({ id: s.id, name: s.name }))}
                value={memberIds}
                onChange={setMemberIds}
              />
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="allocator-label">Allocator</Label>
            <AllocatorSelect value={allocator} onChange={setAllocator} />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="objective-label">
              Objective{" "}
              <span className="text-xs text-muted-foreground">
                (Full mode only)
              </span>
            </Label>
            <ObjectiveSelect value={objective} onChange={setObjective} />
          </div>

          <div className="space-y-1.5">
            <Label>Safety caps</Label>
            <SafetyCapsForm value={caps} onChange={setCaps} />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="capital">Initial capital ($)</Label>
            <Input
              id="capital"
              data-testid="initial-capital"
              type="number"
              min={0}
              step={1000}
              value={capital}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                setCapital(Number.isFinite(v) ? v : 0);
              }}
            />
          </div>
        </CardContent>
      </Card>

      <Button
        data-testid="save-portfolio"
        disabled={!valid || submitting}
        onClick={() => void onSubmit()}
      >
        {submitting ? "Saving..." : "Save Composition"}
      </Button>
    </div>
  );
}
