"use client";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const timeframes = [
  { label: "1D", days: 1 },
  { label: "1W", days: 7 },
  { label: "1M", days: 30 },
  { label: "3M", days: 90 },
  { label: "1Y", days: 365 },
] as const;

export interface SymbolOption {
  value: string;
  label: string;
}

export interface SymbolSelectorProps {
  symbols: SymbolOption[];
  selectedSymbol: string;
  onSymbolChange: (symbol: string) => void;
  selectedTimeframe: number;
  onTimeframeChange: (days: number) => void;
  disabled?: boolean;
}

export function SymbolSelector({
  symbols,
  selectedSymbol,
  onSymbolChange,
  selectedTimeframe,
  onTimeframeChange,
  disabled = false,
}: SymbolSelectorProps): React.ReactElement {
  return (
    <div className="flex flex-wrap items-center gap-4">
      <Select
        value={selectedSymbol}
        onValueChange={onSymbolChange}
        disabled={disabled || symbols.length === 0}
      >
        <SelectTrigger className="w-56">
          <SelectValue
            placeholder={
              symbols.length === 0 ? "No symbols available" : "Select symbol..."
            }
          />
        </SelectTrigger>
        <SelectContent>
          {symbols.map((s) => (
            <SelectItem key={s.value} value={s.value}>
              {s.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className="flex gap-1">
        {timeframes.map((tf) => (
          <Button
            key={tf.label}
            variant={selectedTimeframe === tf.days ? "default" : "outline"}
            size="sm"
            onClick={() => onTimeframeChange(tf.days)}
            disabled={disabled}
          >
            {tf.label}
          </Button>
        ))}
      </div>
    </div>
  );
}
