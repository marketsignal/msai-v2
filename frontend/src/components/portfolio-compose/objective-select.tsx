"use client";

/**
 * Objective selection — matches `PortfolioObjective` enum in
 * `backend/src/msai/models/portfolio_enums.py`.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H3. Only used by Full mode;
 * Quick mode ignores the objective and relies solely on the allocator.
 */

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface ObjectiveOption {
  value: string;
  label: string;
}

const OBJECTIVES: ReadonlyArray<ObjectiveOption> = [
  { value: "maximize_profit", label: "Total return" },
  { value: "maximize_sharpe", label: "Sharpe" },
  { value: "maximize_sortino", label: "Sortino" },
  { value: "maximize_calmar", label: "Calmar" },
  { value: "minimize_max_drawdown", label: "Minimize max drawdown" },
];

interface ObjectiveSelectProps {
  value: string;
  onChange: (value: string) => void;
}

export function ObjectiveSelect({
  value,
  onChange,
}: ObjectiveSelectProps): React.ReactElement {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger data-testid="objective-select" aria-label="Objective">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {OBJECTIVES.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
