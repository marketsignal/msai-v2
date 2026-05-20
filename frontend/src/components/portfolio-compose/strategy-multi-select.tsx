"use client";

/**
 * Searchable multi-select for portfolio strategy membership.
 *
 * Pattern: `Popover` containing a search `Input` + a scrollable list of toggle
 * rows. Selected items render below as removable `Badge` chips.
 *
 * Plan (`docs/plans/portfolio-backtest.md` Task H2) specifies a `Command + Popover`
 * combo. The repo does not currently ship the shadcn `command.tsx` primitive
 * (and `cmdk` is not in `package.json`), so we build the equivalent UX with
 * primitives that DO exist (`popover`, `input`, `button`, `badge`).
 * Same `data-testid="strategy-multi-select"` exposed so the planned E2E specs
 * (Task I1/I2) still bind cleanly.
 */

import { useMemo, useState } from "react";
import { Check, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

export interface StrategyOption {
  id: string;
  name: string;
}

interface StrategyMultiSelectProps {
  options: StrategyOption[];
  value: string[];
  onChange: (next: string[]) => void;
  label?: string;
}

export function StrategyMultiSelect({
  options,
  value,
  onChange,
  label = "Select strategies",
}: StrategyMultiSelectProps): React.ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const [query, setQuery] = useState<string>("");

  const selectedOpts = useMemo(
    () => options.filter((o) => value.includes(o.id)),
    [options, value],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q === "") return options;
    return options.filter(
      (o) => o.name.toLowerCase().includes(q) || o.id.toLowerCase().includes(q),
    );
  }, [options, query]);

  const toggle = (id: string): void => {
    if (value.includes(id)) {
      onChange(value.filter((v) => v !== id));
    } else {
      onChange([...value, id]);
    }
  };

  return (
    <div data-testid="strategy-multi-select" className="space-y-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-label={label}
            aria-expanded={open}
            className="w-full justify-between"
          >
            {selectedOpts.length === 0
              ? label
              : `${selectedOpts.length} selected`}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          className="w-[var(--radix-popover-trigger-width)] p-0"
          align="start"
        >
          <div className="border-b border-border p-2">
            <Input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search strategies..."
              aria-label="Search strategies"
              className="h-8"
            />
          </div>
          <div
            className="max-h-64 overflow-y-auto p-1"
            role="listbox"
            aria-multiselectable="true"
          >
            {filtered.length === 0 ? (
              <p className="px-2 py-3 text-center text-sm text-muted-foreground">
                No strategies found.
              </p>
            ) : (
              filtered.map((o) => {
                const checked = value.includes(o.id);
                return (
                  <button
                    key={o.id}
                    type="button"
                    role="option"
                    aria-selected={checked}
                    onClick={() => toggle(o.id)}
                    data-testid={`strategy-option-${o.id}`}
                    className="flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                  >
                    <span className="truncate">{o.name}</span>
                    {checked && (
                      <Check className="size-4 shrink-0 text-primary" />
                    )}
                  </button>
                );
              })
            )}
          </div>
        </PopoverContent>
      </Popover>

      {selectedOpts.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selectedOpts.map((o) => (
            <Badge
              key={o.id}
              variant="secondary"
              className="gap-1 pr-1"
              data-testid={`strategy-chip-${o.id}`}
            >
              <span>{o.name}</span>
              <button
                type="button"
                aria-label={`Remove ${o.name} strategy`}
                onClick={() => toggle(o.id)}
                className="ml-1 rounded-sm p-0.5 hover:bg-background/50 focus-visible:bg-background/50 focus-visible:outline-none"
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}
