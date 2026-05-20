"use client";

/**
 * Safety caps — three hard-cap numeric inputs sent to the backend as
 * `requested_leverage`, `max_position_size`, `max_drawdown_halt`.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H3. The values bind to
 * `Portfolio` model columns in `backend/src/msai/models/portfolio.py` and are
 * enforced by `services/portfolio_backtest/safety_caps.py`.
 */

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface SafetyCaps {
  max_leverage: number;
  max_position_size: number;
  max_drawdown_halt: number;
}

interface SafetyCapsFormProps {
  value: SafetyCaps;
  onChange: (value: SafetyCaps) => void;
}

/**
 * Parse a string from an `<input type="number">` into a finite number, falling
 * back to ``0`` for empty/NaN. Submission validation lives on the parent page
 * (it blocks submit when any cap is `<= 0`).
 */
function safeFloat(raw: string): number {
  const v = parseFloat(raw);
  return Number.isFinite(v) ? v : 0;
}

export function SafetyCapsForm({
  value,
  onChange,
}: SafetyCapsFormProps): React.ReactElement {
  return (
    <div
      className="grid grid-cols-1 gap-3 sm:grid-cols-3"
      data-testid="safety-caps-form"
    >
      <div className="space-y-1.5">
        <Label htmlFor="max-leverage">Max leverage</Label>
        <Input
          id="max-leverage"
          data-testid="max-leverage"
          type="number"
          min={0.1}
          max={10}
          step={0.1}
          value={value.max_leverage}
          onChange={(e) =>
            onChange({ ...value, max_leverage: safeFloat(e.target.value) })
          }
        />
        <p className="text-xs text-muted-foreground">
          Hard cap; optimizer cannot exceed.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="max-position">Max position size</Label>
        <Input
          id="max-position"
          data-testid="max-position-size"
          type="number"
          min={0}
          max={1}
          step={0.05}
          value={value.max_position_size}
          onChange={(e) =>
            onChange({
              ...value,
              max_position_size: safeFloat(e.target.value),
            })
          }
        />
        <p className="text-xs text-muted-foreground">
          Fraction of capital per position.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="max-dd">Max drawdown halt</Label>
        <Input
          id="max-dd"
          data-testid="max-drawdown-halt"
          type="number"
          min={0}
          max={1}
          step={0.05}
          value={value.max_drawdown_halt}
          onChange={(e) =>
            onChange({
              ...value,
              max_drawdown_halt: safeFloat(e.target.value),
            })
          }
        />
        <p className="text-xs text-muted-foreground">
          Stop trading if portfolio DD exceeds.
        </p>
      </div>
    </div>
  );
}
