"use client";

/**
 * Allocator selection — matches `AllocatorName` enum in
 * `backend/src/msai/models/portfolio_enums.py`.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H3.
 */

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface AllocatorOption {
  value: string;
  label: string;
}

const ALLOCATORS: ReadonlyArray<AllocatorOption> = [
  { value: "equal_weight", label: "Equal weight" },
  { value: "fixed_weight", label: "Fixed weight" },
  { value: "inverse_vol", label: "Inverse volatility" },
  { value: "vol_targeted", label: "Volatility-targeted" },
];

interface AllocatorSelectProps {
  value: string;
  onChange: (value: string) => void;
}

export function AllocatorSelect({
  value,
  onChange,
}: AllocatorSelectProps): React.ReactElement {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger data-testid="allocator-select" aria-label="Allocator">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {ALLOCATORS.map((a) => (
          <SelectItem key={a.value} value={a.value}>
            {a.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
