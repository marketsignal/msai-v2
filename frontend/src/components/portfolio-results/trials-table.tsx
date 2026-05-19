"use client";

/**
 * TrialsTable — sortable table of optimization trials from the Full-mode
 * `optimization_trace`.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. Each row is one
 * trial: `(window_index, params, score)`. The `params` blob is rendered
 * as a compact JSON-ish key=value list because the optimizer's search
 * space is strategy-defined (no fixed schema we can columnize).
 *
 * Columns sortable on click: window, score. Default sort = score desc
 * (best first) so operators see the winning trials immediately.
 */

import { useMemo, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export interface TrialsTableProps {
  /** Raw `PortfolioRun.optimization_trace` payload. ``null`` for Quick. */
  trials: Array<Record<string, unknown>> | null;
  testId?: string;
}

type SortKey = "window" | "score";
type SortDirection = "asc" | "desc";

interface NormalizedTrial {
  index: number;
  window: number | null;
  params: Record<string, unknown>;
  score: number | null;
}

function normalizeTrials(
  raw: Array<Record<string, unknown>> | null,
): NormalizedTrial[] {
  if (!raw) return [];
  return raw.map((row, i) => {
    const rawWindow = row.window_index ?? row.window;
    // The Full-mode optimizer emits ``is_score`` (Optuna's objective —
    // in-sample) and ``oos_score`` (out-of-sample held-out validation)
    // per trial. Earlier reviewers only looked for ``score`` /
    // ``objective_value`` / ``value`` so every trial collapsed to "—".
    // Codex bot iter-4 P2 on PR #73. Prefer ``is_score`` because the
    // table's primary ranking is "which trial Optuna picked," with
    // ``oos_score`` falling back when only OOS was emitted. Legacy
    // keys remain in the chain for back-compat with older traces.
    const rawScore =
      row.is_score ??
      row.oos_score ??
      row.score ??
      row.objective_value ??
      row.value;
    const rawParams = row.params;
    return {
      index: i,
      window: typeof rawWindow === "number" ? rawWindow : null,
      params:
        rawParams && typeof rawParams === "object"
          ? (rawParams as Record<string, unknown>)
          : {},
      score: typeof rawScore === "number" ? rawScore : null,
    };
  });
}

function formatParams(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (entries.length === 0) return "—";
  return entries
    .map(([k, v]) => {
      if (typeof v === "number") return `${k}=${v}`;
      if (typeof v === "string") return `${k}=${v}`;
      return `${k}=${JSON.stringify(v)}`;
    })
    .join(", ");
}

export function TrialsTable({
  trials,
  testId = "trials-table",
}: TrialsTableProps): React.ReactElement {
  const rows = useMemo(() => normalizeTrials(trials), [trials]);
  const [sortKey, setSortKey] = useState<SortKey>("score");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");

  const sortedRows = useMemo(() => {
    const out = [...rows];
    out.sort((a, b) => {
      const va = sortKey === "window" ? a.window : a.score;
      const vb = sortKey === "window" ? b.window : b.score;
      // Push nulls to the bottom regardless of direction.
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return sortDir === "asc" ? va - vb : vb - va;
    });
    return out;
  }, [rows, sortKey, sortDir]);

  const toggleSort = (key: SortKey): void => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const arrow = (key: SortKey): string => {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  };

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">Optimization Trials</CardTitle>
        <CardDescription>
          Walk-forward optimizer trace — one row per trial × window
        </CardDescription>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No optimization trace recorded. This is expected for Quick-mode runs
            and for Full runs that errored before the first trial.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[80px]">#</TableHead>
                  <TableHead
                    className="w-[120px] cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("window")}
                  >
                    Window{arrow("window")}
                  </TableHead>
                  <TableHead>Params</TableHead>
                  <TableHead
                    className="w-[120px] cursor-pointer select-none text-right hover:text-foreground"
                    onClick={() => toggleSort("score")}
                  >
                    Score{arrow("score")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedRows.map((row) => (
                  <TableRow
                    key={row.index}
                    data-testid={`trial-row-${row.index}`}
                  >
                    <TableCell className="font-mono text-xs text-muted-foreground tabular-nums">
                      {row.index}
                    </TableCell>
                    <TableCell className="font-mono text-xs tabular-nums">
                      {row.window ?? "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {formatParams(row.params)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.score === null ? "—" : row.score.toFixed(4)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
